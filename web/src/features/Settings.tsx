import { usePrefs, type Preferences } from '../state/prefs';
import { Field, ui } from '../ui';
import ProductDefaults from './ProductDefaults';
export default function Settings() {
  const prefs = usePrefs();
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>PREFERENCES / 05</div>
          <h1>产品与界面设置</h1>
          <p>产品默认设置在本机共享，界面偏好保存在当前浏览器。</p>
        </div>
      </div>
      <ProductDefaults />
      <section className={ui.section}>
        <div className={ui.formGrid}>
          <Field id="theme" label="外观">
            <select
              id="theme"
              value={prefs.theme}
              onChange={(e) => prefs.update({ theme: e.target.value as Preferences['theme'] })}
            >
              <option value="light">浅色</option>
              <option value="dark">深色</option>
              <option value="system">跟随系统</option>
            </select>
          </Field>
          <Field id="density" label="工作台密度">
            <select
              id="density"
              value={prefs.density}
              onChange={(e) => prefs.update({ density: e.target.value as Preferences['density'] })}
            >
              <option value="compact">紧凑</option>
              <option value="comfortable">舒适</option>
            </select>
          </Field>
        </div>
        <div className={ui.stack}>
          <label className={ui.row}>
            <input
              type="checkbox"
              checked={prefs.shortcuts}
              onChange={(e) => prefs.update({ shortcuts: e.target.checked })}
            />
            启用字母快捷键（K 保留 / X 剔除 / U 未审 / B 背景）
          </label>
          <label className={ui.row}>
            <input
              type="checkbox"
              checked={prefs.autoAdvance}
              onChange={(e) => prefs.update({ autoAdvance: e.target.checked })}
            />
            审核单张图片后自动前进
          </label>
          <p className={ui.muted}>
            在输入框内不会触发图片审核快捷键。Ctrl + S 保存标注，Ctrl + Z 撤销最近一次审核。
          </p>
        </div>
      </section>
    </div>
  );
}
