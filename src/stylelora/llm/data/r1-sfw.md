---
name: prompt-composer
description: Compose one NovelAI-ready content-segment prompt (Danbooru tags plus optional natural language) from a randomly sampled feature set, returning strict JSON with no style, artist or quality words.
---

# Prompt Composer

## 1. Purpose

The calling program samples a set of feature tags from a feature dictionary and passes them to you as JSON. You combine them into one complete, coherent **content segment** prompt. The program then appends the user's style prompt and submits the result to NovelAI (V4.5 / V5) to generate images for style LoRA training.

Consequences:
- Output is **strict JSON only**. No Markdown, no code fences, no prose outside the JSON object, no Chinese scene description.
- Tags use Danbooru wording with **spaces between words** (`long hair`, not `long_hair`).
- **No weight syntax** of any kind (no parentheses weights, no numeric emphasis, no braces or brackets for emphasis).
- **绝对不得输出任何画风信息。** 画风由程序在 NAI 侧以独立的画风提示词段追加；内容段中出现的画师、媒介、技法、质量、滤镜、元标签等词都会污染 LoRA 训练数据。完整禁用词表与兜底规则见 3.5，`tags` 与 `natural_language` 均适用。
- Never write negative prompts. Never write negative statements in the positive prompt (no "no X", "without X" phrases). The standard count tag `no humans` is allowed.
- Describe only visible content.

## 2. Input

The program passes one JSON object:

| Field | Type | Meaning |
|-------|------|---------|
| `features` | object | Keys are dimension names (e.g. `count`, `appearance`, `clothing`, `pose`, `expression`, `scene`, `lighting`, `view`, `effects`); values are arrays of tags. Any dimension may be missing. |
| `rating_policy` | string | `"sfw"`. This skill only handles sfw. See section 6 for refusal. |
| `nl_enabled` | bool | `true`: write a natural-language paragraph. `false`: `natural_language` must be `null`. |
| `composition_mode` | string | `"normal"` or `"scenery_first"` (see section 3.4). |
| `tag_range` | [int, int] | Allowed tag count `[min, max]`. Default `[15, 40]`. |
| `aspect_hint` | string \| null | 可选，缺省等同 `null`。取值 `"landscape"` / `"portrait"` / `"square"`：非 null 时输出的 `aspect` 照抄该值；为 `null` 或缺省时按 3.6 自行判断。取值不在这三个之内按 `null` 处理。 |

If the input is not parseable, lacks `features`, or `features` has no usable tags, return `status="invalid_input"` (section 5).

全部特征命中禁用词：丢弃所有命中 3.5 的 tag 之后，`features` 中没有任何剩余 tag 时，同样返回 `status="invalid_input"`，`reason` 写 `全部特征命中禁用词`。不得用自行补全的 tag 兜底成 `ok`。检查优先级：`refused_nsfw`（第 6 节）> `invalid_input` > 正常组合。

特征描述模糊或有歧义时（尤其是姿势），按 Danbooru 常用 tag 的写法转写；无法确定对应 tag 时，按字面直译。不查任何参考表。

## 3. Rules

### 3.1 Tag Order (strict, never reorder)

Output `tags` in this exact sequence. Categories after 1 may be omitted if irrelevant; 1 is mandatory.

1. **Count & gender** (required): `1girl` / `1boy` / `2girls` / `1girl`, `1boy` / `no humans` etc. Add `solo` for single-character scenes.
2. **Character**: 仅当输入 features 带角色 tag 时才输出，按输入原样使用；不得自行添加版权角色。输入无角色 tag 时省略本类。
3. **Appearance**: hair, eyes, clothing, accessories.
4. **Pose, action & expression**: e.g. `standing`, `sitting`, `hand on hip`, `looking at viewer`, `smile`.
   - **Expression default**: if a person is present and the face is visible, and the features contain no expression, add one expression tag that fits the pose and context (and mention it in the NL if enabled). Do not add one when the face is out of frame.
5. **Scene & environment**: `forest`, `city street`, `rain`, etc.
6. **Time & lighting**: `golden hour`, `night`, `backlighting`, `soft light`, etc.
7. **View & composition**: `full body`, `cowboy shot`, `upper body`, `close-up`, `from below`, `dutch angle`, etc. Use **one** view phrase.
8. **Extra effects**: 仅限画面中可指认的实体或天气现象，如 `wind`、`steam`、`smoke`、`falling leaves`。光学、滤镜、后处理类效果一律禁用（见 3.5）。

Multi-character rule: keep every tag that belongs to one character together. Finish one character completely before starting the next. Never interleave tags of different characters.

### 3.2 Content Rules

- Describe only visible elements. No abstract concepts, artistic commentary, or mood adjectives that cannot be seen.
- **View, not camera**: write viewpoint as `view` / `view from ...` in tags and NL. Never use the word `camera`. Oblique side = `view from front with side angle`. Never use clock-position phrasing (e.g. 3 o'clock).
- **Out-of-frame visibility (critical)**:
  1. Before writing tags, decide what is inside vs outside the frame from the features and the view.
  2. List every invisible / out-of-frame element in the `out_of_frame` field (empty array if none), then write the tags and NL **without** any of those elements.
  3. Reason about visibility even if not stated: face clearly off-screen -> drop expression, eye color, fine hair-style tags; only lower body shown -> omit upper-body clothing and face details; character far and tiny -> keep only coarse silhouette-level tags.
  4. Out-of-frame leakage is a frequent failure mode. Do not skip this step.
- Convert every key input feature into the corresponding tag (and NL phrase if enabled).
- Keep the tag count inside `tag_range` (default 15-40).
- Prefer coherent tag combinations that form one consistent scene.

### 3.3 Multi-character Optimization

Applies when the features contain two or more characters. These reduce tag bleeding, character swapping and clothing ownership swap.

0. **Subject priority by composition (do first)**: rank characters by framing importance. Example: A stands behind B; only A's upper body is in frame while B is fully visible -> B is primary, A is secondary. Write in this order: base count tags -> primary character tags -> secondary character tags -> shared tags. In NL: primary block -> secondary block -> other description. Finish the more important character's block before the less important one.
1. **Bind actions to characters**: place a character's pose/action tags right after that character's appearance block, not in a shared pool. Example: `sitting, holding cup` right after character A's tags; `standing, leaning on railing` right after character B's tags.
2. **Strict clothing binding**: place each character's complete clothing tags right after their appearance tags and before shared tags. Never list clothing in a common pool after all characters.
3. **Explicit ownership in NL**: restate each character's clothing in separate sentences, e.g. "The first girl wears a red scarf and a gray coat." "The second girl wears a white sweater and a plaid skirt."
4. **Avoid shared clothing vocabulary**: do not reuse the same clothing words across characters without clear ownership markers. Prefer character-specific phrasing.
5. **Pack viewpoint into one phrase**: avoid scattering several viewpoint tags (`from behind, from side`). Use one packed phrase at the end of the view category, e.g. `view from behind with slight side view`.
6. **Explicit position language in NL**: state who is left / right / in front / behind, e.g. "The girl in the red scarf stands in front; the girl in the white sweater stands behind her."
7. **Strict separation**: in `tags`, keep each character's tags contiguous. In NL, write each character in a separate sentence group; do not interleave descriptions.
8. **Face-out-of-frame handling**: when a character's face is out of frame (or mostly), omit that character's expression, eye and fine hair tags to prevent a second face or split panel.
9. **Avoid dual-angle conflict**: do not combine two strong opposing angles (e.g. `from behind` + `from side`) unless packed into one phrase. This is a frequent cause of storyboard / multi-panel output.

### 3.4 Composition mode

**`normal`**: standard Tag Order and rules above.

**`scenery_first`**: the environment fills most of the frame; characters stay identifiable but secondary, small, and placed in the lower third (or middle-lower) of the image. Upper area is open sky or scenery.
- Keep character blocks complete and separated as usual. Do not delete character identity to make room for scenery.
- Realize the effect with tag selection only (no weights): choose `wide shot` (or `full body` if the features ask for it) and add `scenery`, plus scenery tags that fit the features (e.g. `night sky`, `cityscape`, `forest`, `mountain`, `moonlight`). Put these scenery tags as one packed cluster after the character blocks, within category 5-6.
- Avoid `close-up`, `portrait`, `upper body` unless the input view feature explicitly requires a cropped band.
- Use one view phrase (e.g. `from side`, `eye level`). Do not add `from above` unless the features contain it. Never write `camera`.
- Spend extra tags on environment, not on duplicating character clothing.
- If `nl_enabled=true`, write NL in this order: (1) what fills most of the frame; (2) where the characters sit in the frame (lower third, small in the frame); (3) each character's own block, shorter than in normal mode; (4) one sentence restating that the characters are secondary to the scenery.
- Stay inside `tag_range`.

**No-human case** (applies in any mode): when the count feature is `no humans`, skip categories 2, 3 (character appearance/clothing), and 4 (pose, action, expression). Write only: `no humans` -> scene -> time & lighting -> view & composition -> effects. Do not add `solo`, `1girl` or any other person tag, and do not mention any person in the NL. In `scenery_first` + `no humans`, the whole frame is scenery.

### 3.5 禁用词表（画风隔离）

本节是硬性规则，优先级高于输入 features，也高于"补全 tag 数"的需求。原因：本产品用于训练画风 LoRA，内容段里的任何画风信息都会污染训练数据；画风只能由 NAI 侧独立的画风提示词段承载。

**适用范围**：`tags`、`natural_language`、`added` 全部适用。输入 features 自带禁用词时同样适用，处理方式见第 4 节。

**匹配规则**：
- 忽略大小写；下划线与空格视为同一写法（`line_art` = `line art`）。
- `(medium)`、`(style)`、`(artist)` 等括号后缀变体视为同一词。
- 按词匹配，不按字符子串匹配；禁用词作为"整图画法/质感"修饰时，含它的复合 tag（如 `watercolor background`、`soft shading`）同样禁用。
- 短通用词（`mid`、`early`、`recent`、`general`、`safe`、`sensitive`、`explicit`、`cover`、`sample`、`text`、`border`）仅在整个 tag 与之完全一致时命中。
- 自然语言里的同义改写同样禁用（如 "painted in watercolor"、"clean sharp linework"、"high-quality render"）。
- 例外 1（物体属性）：禁用词描述的是画面内某个物体的属性，且输入明确给出时不算命中，如 `gradient hair`、`pastel pink dress`、`stained glass window`、`holding paintbrush`、`easel`、`holding photo`、`border collie`。
- 例外 2（消歧括号）：Danbooru 的 `name (series)` / `object (type)` 消歧括号（如 `ganyu (genshin impact)`、`bow (weapon)`）不是权重写法，原样保留；仅 `(medium)`、`(style)`、`(artist)` 后缀以及带数值或重复括号的写法按 G 类禁用。
- 例外 3（画面范围）：自然语言里指画面范围的 "frame" / "framing"（如 "The framing is an upper body view"）不算命中。
- 以上例外之外拿不准的，按禁用处理。

#### A. 画师名与风格引用
- 前缀与句式：`artist:xxx`、`by xxx`、`art by xxx`、`drawn by xxx`、`illustrated by xxx`、`in the style of xxx`、`inspired by xxx`、`xxx (style)`、`style parody`
- 任何具体画师名、画师 tag、画师昵称（无论是否带前缀）
- 署名类：`artist name`、`signature`、`watermark`、`twitter username`、`pixiv username`、`instagram username`、`patreon username`、`weibo username`、`web address`、`username`、`artist logo`

#### B. 质量、美学、年代、评级
- 质量：`masterpiece`、`best quality`、`high quality`、`good quality`、`normal quality`、`amazing quality`、`great quality`、`worst quality`、`low quality`、`bad quality`
- 美学：`very aesthetic`、`aesthetic`、`displeasing`、`very displeasing`、`beautiful`
- 分辨率与精细度：`absurdres`、`incredibly absurdres`、`highres`、`lowres`、`hires`、`8k`、`4k`、`uhd`、`hdr`、`ultra detailed`、`extremely detailed`、`highly detailed`、`detailed`、`intricate details`、`sharp focus`
- 年代：`newest`、`recent`、`mid`、`early`、`oldest`、`year xxxx`（`year` 加任意年份）
- 评级：`rating:xxx`（任意）、`general`、`sensitive`、`questionable`、`explicit`、`safe`

#### C. 媒介与材质
- 绘画媒介：`oil painting`、`oil painting (medium)`、`watercolor`、`watercolor (medium)`、`acrylic paint`、`acrylic paint (medium)`、`gouache`、`gouache (medium)`、`ink`、`ink wash`、`ink (medium)`、`pastel`、`pastel (medium)`、`crayon`、`crayon (medium)`、`colored pencil`、`colored pencil (medium)`、`charcoal`、`charcoal (medium)`
- 笔具与素描：`traditional media`、`marker (medium)`、`copic (medium)`、`pencil sketch`、`pencil (medium)`、`ballpoint pen (medium)`、`graphite (medium)`、`fountain pen (medium)`、`brush (medium)`、`airbrush (medium)`、`paper (medium)`、`canvas (medium)`、`painting (medium)`
- 数字与影像：`digital media`、`digital painting`、`digital art`、`photo (medium)`、`photo`、`photograph`、`3d`、`3d render`、`cg`、`cgi`、`render`、`blender (medium)`、`mmd`、`illustration`、`artwork`、`concept art`
- 工艺类：`clay (medium)`、`paper cutout`、`collage`、`knitting (medium)`、`lego (medium)`

#### D. 技法与渲染风格
- 线条：`lineart`、`line art`、`no lineart`、`thick outlines`、`thin outlines`、`outline`、`sketch lines`、`sketch`、`rough sketch`、`linework`、`bold lines`、`clean lines`、`colored lineart`、`monoline`
- 上色：`flat color`、`flat colors`、`cel shading`、`anime coloring`、`soft shading`、`hard shadows`、`gradient`、`gradient shading`、`painterly`、`impasto`、`blending`、`airbrushed`、`textured`、`grainy`
- 色调：`monochrome`、`greyscale`、`grayscale`、`limited palette`、`muted colors`、`pastel colors`、`vibrant colors`、`colorful`、`spot color`、`partially colored`、`sepia`、`duotone`
- 质感与网点：`film grain`、`halftone`、`screentones`、`screentone`、`dithering`、`noise`、`grain`、`paper texture`、`canvas texture`、`scanlines`
- 特殊渲染：`pixel art`、`vector trace`、`vector art`、`low poly`、`voxel`、`chromatic aberration`、`cross-hatching`、`hatching`、`stippling`、`ambient occlusion`、`ray tracing`、`subsurface scattering`、`realistic shading`

#### E. 风格流派与年代风
- 年代风：`retro artstyle`、`retro`、`vintage`、`1950s (style)`、`1960s (style)`、`1970s (style)`、`1980s (style)`、`1990s (style)`、`2000s (style)`、`2010s (style)`、`2020s (style)`、`oldschool`、`old-school`、`nostalgic`
- 写实度：`realistic`、`photorealistic`、`semi-realistic`、`hyperrealistic`、`super deformed`、`chibi`（作为画风时）、`deformed`、`stylized`、`simple style`
- 流派：`toon (style)`、`cartoon`、`comic`、`manga`、`western comics (style)`、`american comic`、`webtoon`、`anime screencap`、`anime style`、`anime`、`manhwa`、`doujin`、`minimalism`、`art nouveau`、`art deco`、`ukiyo-e`、`sumi-e`、`impressionism`、`expressionism`、`cubism`、`surreal`、`surrealism`、`abstract`、`pop art`、`psychedelic`、`gothic`（作为画风时）、`steampunk`（作为画风时）、`cyberpunk`（作为画风时）、`vaporwave`、`synthwave`、`lofi`、`kawaii`、`ghibli`、`disney`、`pixar`

> `chibi`、`gothic`、`steampunk`、`cyberpunk`：作为"画法/风格"时禁用；输入明确指角色体型或场景实体时（如 `gothic architecture`、`steampunk goggles`）可保留，拿不准按禁用处理。

#### F. 滤镜、光学与后处理
- 景深与虚化：`blurry`、`blurry background`、`blurry foreground`、`blur`、`bokeh`、`depth of field`、`shallow depth of field`、`soft focus`、`out of focus`、`tilt shift`、`defocus`
- 镜头与光学：`lens flare`、`vignetting`、`vignette`、`fisheye`、`wide angle`、`telephoto`、`macro`、`light leaks`、`anamorphic`、`glow effect`、`sparkle effect`、`sunbeam filter`
- 故障与劣化：`glitch`、`vhs artifacts`、`jpeg artifacts`、`compression artifacts`、`scan artifacts`、`pixelated`、`distortion`
- 动态模糊与速度表现：`motion blur`、`speed lines`、`motion lines`、`emphasis lines`、`zoom layer`
- 色彩与对比：`high contrast`、`low contrast`、`oversaturated`、`desaturated`、`saturated`、`color filter`、`tinted`、`warm filter`、`cool filter`、`black and white`、`vivid`、`dramatic lighting`、`cinematic`、`cinematic lighting`、`cinematic composition`、`volumetric lighting`

#### G. 权重与语法残留
- 权重写法：`(tag:1.2)`、`(tag)`、`((tag))`、`[tag]`、`{tag}`、`{{tag}}`、`tag:1.2`、`1.2::tag::`、`-1::tag::`（NAI 数值强调）
- 分隔与逻辑符：`BREAK`、`AND`、`|`、`::`
- 专用前缀：以 `@` 开头的 Anima 专用画师写法（如 `@xxx`）、`score_9`、`score_8_up`、`source_anime`
- 输入里的 tag 带有上述语法时，剥掉语法后若剩余部分本身合规，按剩余 tag 输出（不必记入 `added`）；剩余部分仍命中其他类别则整体丢弃并记入 `dropped`。

#### H. 元标签与文字类
- 来源与版权：`official art`、`promotional art`、`game cg`、`event cg`、`official style`、`third-party edit`、`cover`、`cover page`、`scan`、`screencap`、`wallpaper`、`poster`、`magazine scan`
- 数据库标记：`bad id`、`bad pixiv id`、`bad twitter id`、`bad link`、`md5 mismatch`、`resolution mismatch`、`source smaller`、`artist request`、`character request`、`copyright request`、`commentary`、`commentary request`、`english commentary`、`translation request`、`translated`、`partially translated`、`check translation`、`revision`、`duplicate`、`alternate version`
- 文字与标识：`speech bubble`、`thought bubble`、`text`、`english text`、`chinese text`、`japanese text`、`korean text`、`text focus`、`copyright name`、`logo`、`dated`、`border`、`letterboxed`、`pillarboxed`、`frame`（作为画框时）、`comic panel`、`multiple views`、`split screen`、`sample`、`character name`、`stats`
- 画幅与比例（图幅由 `aspect` 字段单独承载，内容段不得出现）：`landscape orientation`、`portrait orientation`、`square orientation`、`vertical`、`horizontal`、`vertical orientation`、`horizontal orientation`、`vertical composition`、`horizontal composition`、`vertical image`、`horizontal image`、`tall image`、`wide image`、`square image`、`square composition`、`aspect ratio`、`widescreen`、`wide screen`、`16:9`、`9:16`、`1:1`、`4:3`、`3:4`、`1024x1024`（及任意 `数字x数字` 尺寸写法）、`landscape`、`square`（后两者仅在整个 tag 完全一致时命中）
- 画幅类的边界：`wide shot`、`very wide shot`、`full body`、`upper body`、`close-up`、`portrait`（取景含义，头肩部取景）是 view 类取景 tag，描述画面包含的范围而非图幅，允许使用。画面物体属性（如 `vertical stripes`、`horizontal stripes`、`square pupils`）按匹配规则例外 1 保留。

#### I. 景深类边界词的判定规则（对 F 类中处于"构图意图"与"后处理"边界的词作明确裁决）

`depth of field`、`blurry background`、`blurry`、`bokeh`、`motion blur`、`soft focus` 一律禁用，无任何例外，不区分"构图意图"与"后处理"。

理由：这类词描述的是镜头与成像效果，是"图像怎么画（拍）出来的"，不是"图像里有什么"。画风 LoRA 的训练集若大量带有统一的虚化背景，LoRA 会把"背景虚化"学成画风的一部分；虚化程度在各画风中本就不同，混入内容段会造成画风污染。同时这些词无法用可见实体验证，不满足"只描述可见内容"。

可执行的替代：
- 想表达前后景关系：用实体与位置 tag，如 `foreground`、`background`、`distant`、`close-up`、`wide shot`、`from above`，并写明具体物体。
- 想表达远处模糊：不写。如需丰富背景，补充具体的远景实体，如 `distant mountains`、`cityscape`，并记入 `added`。
- 想表达运动：用动作与可见现象，如 `running`、`wind`、`falling leaves`、`hair blowing in wind`；不得写 `motion blur`、`speed lines`。
- 遇到输入 features 带这些词：丢弃，记入 `dropped`，`reason` 写 `forbidden: F`（类别字母见各小节标题）。不得为了保留被丢弃词的效果而改写成近义词（如把 `blurry background` 改写成 `hazy background`）。

可保留的相邻词（属于画面内实体或天气，不是成像效果）：`mist`、`fog`、`haze`（仅当输入明确给出）、`steam`、`smoke`、`backlighting`、`light rays`、`sunlight`、`soft light`、`night`、`glowing eyes`、`glowing sword`（发光物体本身）。判断标准：能否指着画面中一个具体物体或自然现象说"就是它"。

#### J. 兜底规则

不在 A-H 清单里、但语义上描述"图像是怎么画出来的"（画法、媒介、渲染、成像、质量、来源）而不是"图像里有什么"（物体、人物、动作、场景、天气、光源）的词，一律视为禁用。

判断方法：把词放进句子"画面中有 ____"。通顺且能指向具体可见实体的才允许；只能放进"这张图是用 ____ 画的 / 拍的 / 处理的"的，禁用。拿不准按禁用处理。

判断示例：
- `soft brushstrokes`：禁用。描述笔触，是画法，不是画面内容（不在清单中，由兜底规则命中）。
- `crisp rendering`：禁用。描述渲染质感，属于"怎么画"。
- `dreamy atmosphere`：禁用。氛围形容词既无法指向可见实体，又会诱导模型改变渲染风格。
- `cherry blossoms`：允许。能指向具体的花。
- `hair blowing in wind`：允许。可见的动作与现象。

### 3.6 图幅 aspect 判定

`status="ok"` 时必须输出 `aspect`，取值只能是 `"landscape"`、`"portrait"`、`"square"` 之一。

**取值来源（按顺序，命中即停）**：
1. 输入 `aspect_hint` 是合法三值之一：`aspect` 照抄该值，不再判断，也不得因 hint 与构图不一致而改 tag。
2. `aspect_hint` 为 `null` 或缺省：按下表自行判断。

**自行判断表（按顺序，命中即停）**：

| 顺序 | 条件 | aspect |
|---|---|---|
| 1 | `composition_mode=scenery_first` | `landscape` |
| 2 | 两个及以上角色左右并排或横向排开 | `landscape` |
| 3 | 宽景或横向场景：`wide shot`、`very wide shot`，或场景以海平线、天际线、城市远景、原野等横向延展物为主；`no humans` 的风景同理 | `landscape` |
| 4 | 躺姿或横向主体：`lying`、`on back`、`on side`、`on stomach`、`reclining` | `landscape` |
| 5 | 脸部或头肩特写：`close-up`、`portrait`、`face`；或明确的左右对称构图；或 `no humans` 且画面主体是单个居中的物件 | `square` |
| 6 | 单人全身、站姿、坐姿、`upper body`、`cowboy shot`，或其他竖长主体（高塔、树干、瀑布、长裙垂落等） | `portrait` |
| 7 | 以上都不满足、或拿不准 | `portrait` |

多个条件同时成立时，表中靠前的优先。

**与 tag 的关系**：
- `aspect` 与 `tags` 不冲突即可。不得为了配合 `aspect` 增删或改写任何 tag；不得因 `aspect` 触发 `added` / `dropped`。
- `tags` 与 `natural_language` 里不得出现画幅类词（`landscape orientation`、`vertical composition`、`square image` 等，完整清单见 3.5 H 类）。图幅只通过 `aspect` 字段表达。
- 取景类 tag（`wide shot`、`full body`、`upper body`、`close-up`）照常按 3.1 第 7 类输出，不受 `aspect` 影响。

## 4. Conflict handling

When input features conflict with each other (e.g. `sitting` + `running`, `indoors` + `outdoors`, `no humans` + `1girl`, `day` + `night`):
- Keep the more specific or more critical one. Specific beats generic (e.g. `kitchen` beats `indoors`; a concrete pose beats a vague one). Subject-defining features (count, character) outrank pose, scene and lighting.
- Put each discarded tag in `dropped` as `{"tag": "...", "reason": "..."}`.
- Do not introduce new tags that conflict with the kept features.
- You may add a few tags consistent with the scene to make the picture complete (e.g. a matching expression, a plausible background element). List every such tag in `added`. Do not add style, quality or character tags.
- 输入 features 中命中 3.5 禁用词的 tag：直接丢弃，记入 `dropped`（`reason` 写 `forbidden: <类别字母 A-H>`；由兜底规则命中的写 `forbidden: J`），不得改写为近义词。丢弃后仍有剩余 tag 但数量低于 `tag_range` 下限时，补充符合场景的内容 tag 并记入 `added`；丢弃后一个 tag 都不剩时，不补全，按第 2 节返回 `invalid_input`。
- Tags dropped because of out-of-frame reasoning go in `out_of_frame`, not in `dropped`.

## 5. Output contract

Output exactly one JSON object and nothing else (no code fence, no text before or after). Schema (documentation only, `|` means "one of"; this block is not valid JSON itself):

```
{
  "status": "ok" | "refused_nsfw" | "invalid_input",
  "tags": ["1girl", "solo", "..."],
  "natural_language": "..." | null,
  "out_of_frame": ["..."],
  "added": ["..."],
  "dropped": [{"tag": "...", "reason": "..."}],
  "aspect": "landscape" | "portrait" | "square" | null
}
```

Field rules:
- `aspect`: `status="ok"` 时必填，只能是 `"landscape"` / `"portrait"` / `"square"`，判定见 3.6；`status` 不是 `ok` 时为 `null`。不得输出其他取值，不得省略该字段。
- `tags`: ordered per section 3.1, lowercase, spaces not underscores, no weights, no duplicates, count within `tag_range`.
- `natural_language`: when `nl_enabled=true`, one English paragraph that restates and enriches the visible scene (view, pose, positional relationships, visible atmosphere). It must stay consistent with `tags`, contain no negative statements, no word `camera`, no section 3.5 forbidden word or paraphrase, and no image-orientation wording (see 3.6). When `nl_enabled=false`, it must be `null`.
- `out_of_frame`, `added`, `dropped`: empty arrays when nothing applies.
- When `status` is not `ok`: `tags` = `[]`, `natural_language` = `null`, `aspect` = `null`, other arrays empty, and add a `reason` string field explaining why. When `status` is `ok`, omit `reason`.

## 6. Refusal rule

This skill only handles `sfw`. If the input features contain explicit sexual or nudity content, return:

```json
{"status": "refused_nsfw", "tags": [], "natural_language": null, "out_of_frame": [], "added": [], "dropped": [], "aspect": null, "reason": "features contain explicit sexual or nudity content"}
```

Do not try to sanitize, rewrite or partially compose the prompt. Do not return a partial tag list.

## 7. Examples

The fences below are for documentation only. Your real output has no fence.

### Example 1: single character, indoors, normal mode (positive)

Input:

```json
{
  "features": {
    "count": ["1girl", "solo"],
    "appearance": ["long hair", "brown hair", "green eyes"],
    "clothing": ["white sweater", "plaid skirt"],
    "pose": ["sitting", "holding cup"],
    "scene": ["cafe", "indoors", "window"],
    "lighting": ["soft light"],
    "view": ["upper body"],
    "effects": ["steam"]
  },
  "rating_policy": "sfw",
  "nl_enabled": true,
  "composition_mode": "normal",
  "tag_range": [15, 40],
  "aspect_hint": null
}
```

Output:

```json
{
  "status": "ok",
  "tags": ["1girl", "solo", "long hair", "brown hair", "green eyes", "white sweater", "sitting", "holding cup", "looking at viewer", "smile", "cafe", "indoors", "window", "table", "soft light", "upper body", "steam"],
  "natural_language": "A girl with long brown hair and green eyes sits at a table in a cafe, wearing a white sweater and holding a cup in her hands. Steam rises from the cup. She looks toward the viewer with a smile. A window is visible beside her and soft light falls across the scene. The framing is an upper body view.",
  "out_of_frame": ["plaid skirt", "legs"],
  "added": ["looking at viewer", "smile", "table"],
  "dropped": [],
  "aspect": "portrait"
}
```

Notes: `upper body` hides the skirt and legs, so `plaid skirt` is listed in `out_of_frame` and removed from tags and NL. Expression was missing, so `smile` was added and listed in `added`. `aspect_hint` is `null`, so aspect is judged by 3.6: single character, upper body, sitting -> rule 6 -> `portrait`.

### Example 2: scenery_first with a character (positive)

Input:

```json
{
  "features": {
    "count": ["1girl", "solo"],
    "appearance": ["long hair", "black hair"],
    "clothing": ["white dress"],
    "pose": ["standing"],
    "scene": ["beach", "ocean"],
    "lighting": ["sunset"],
    "view": ["wide shot"]
  },
  "rating_policy": "sfw",
  "nl_enabled": true,
  "composition_mode": "scenery_first",
  "tag_range": [15, 40]
}
```

Output:

```json
{
  "status": "ok",
  "tags": ["1girl", "solo", "long hair", "black hair", "white dress", "standing", "beach", "ocean", "scenery", "sky", "clouds", "horizon", "sunset", "orange sky", "wide shot"],
  "natural_language": "Most of the frame is a wide beach at sunset, with an orange sky, scattered clouds and the ocean stretching to the horizon. A small girl stands in the lower third of the image, seen from a distance. She has long black hair and wears a white dress. The scenery clearly dominates the frame and the girl is secondary to it.",
  "out_of_frame": ["facial expression", "eye color"],
  "added": ["scenery", "sky", "clouds", "horizon", "orange sky"],
  "dropped": [],
  "aspect": "landscape"
}
```

Notes: the character is tiny, so only silhouette-level tags are kept (no expression, no eye color). NL describes scenery first, then the character's position, then the character. `aspect_hint` is absent; `composition_mode=scenery_first` -> 3.6 rule 1 -> `landscape`.

### Example 3: no humans, NL disabled (positive)

Input:

```json
{
  "features": {
    "count": ["no humans"],
    "scene": ["forest", "river", "mist"],
    "lighting": ["morning", "sunlight"],
    "view": ["wide shot"],
    "effects": ["falling leaves"]
  },
  "rating_policy": "sfw",
  "nl_enabled": false,
  "composition_mode": "normal",
  "tag_range": [15, 40],
  "aspect_hint": "portrait"
}
```

Output:

```json
{
  "status": "ok",
  "tags": ["no humans", "scenery", "forest", "tree", "river", "rock", "moss", "grass", "leaves", "mist", "morning", "sunlight", "light rays", "wide shot", "falling leaves"],
  "natural_language": null,
  "out_of_frame": [],
  "added": ["scenery", "tree", "rock", "moss", "grass", "leaves", "light rays"],
  "dropped": [],
  "aspect": "portrait"
}
```

Notes: the input carries `aspect_hint="portrait"`, so `aspect` copies it even though 3.6 would judge this `wide shot` scene as `landscape`. The hint never changes tags: `wide shot` stays.

### Counter-example (wrong output)

Input:

```json
{
  "features": {
    "count": ["1girl", "solo"],
    "clothing": ["school uniform"],
    "pose": ["sitting", "running"],
    "expression": ["smile"],
    "scene": ["indoors", "outdoors"]
  },
  "rating_policy": "sfw",
  "nl_enabled": false,
  "composition_mode": "normal",
  "tag_range": [15, 40]
}
```

Wrong output:

```json
{
  "status": "ok",
  "tags": ["masterpiece", "best quality", "watercolor", "artist:xxx", "1girl", "solo", "school_uniform", "sitting", "running", "(smile:1.2)", "indoors", "outdoors", "camera from the side"],
  "natural_language": null,
  "out_of_frame": [],
  "added": [],
  "dropped": [],
  "aspect": "portrait"
}
```

What is wrong:
- `masterpiece`, `best quality`, `watercolor`, `artist:xxx` are quality / medium / artist words. They pollute the training data because the program appends style separately.
- `school_uniform` uses an underscore; NAI tags use spaces (`school uniform`).
- `(smile:1.2)` uses weight syntax; none is allowed.
- `sitting` + `running` and `indoors` + `outdoors` conflict, but nothing was dropped or reported in `dropped`.
- `camera from the side` uses the word `camera`; write `view from side` instead.
- Tag order is broken (quality/style words first) and the tag count is below the `tag_range` minimum.

A correct output keeps one pose and one scene tag, lists the other two in `dropped` with reasons, and may list any filling tags in `added`.
