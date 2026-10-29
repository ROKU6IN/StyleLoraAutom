import { request } from '../../api/client';
import { errorText } from '../../api/errors';
import type { ImageItem } from '../../api/types';
import { useAction } from '../../hooks/useAction';
import { queryClient } from '../../state/queryClient';
import { canWrite, useSession } from '../../state/session';
import { ErrorBox, Field } from '../../ui';

export function ImageRating({ pid, image }: { pid: string; image: ImageItem }) {
  const action = useAction();
  useSession((s) => s.connection);
  return (
    <>
      <Field id="image-rating" label="图片评级" hint="选择后立即保存为人工评级；正在推理的旧结果将不再写入。">
        <select
          id="image-rating"
          value={image.rating ?? ''}
          disabled={action.busy || image.file_missing || !canWrite()}
          onChange={(e) => {
            const rating = e.target.value;
            void action.run(async () => {
              try {
                await request(
                  'PATCH',
                  `/api/projects/${pid}/images/${image.image_id}`,
                  { rating },
                  { version: image.version },
                );
              } finally {
                await queryClient.invalidateQueries({ queryKey: ['project', pid] });
              }
            });
          }}
        >
          <option value="" disabled>
            尚未确定
          </option>
          <option value="general">general</option>
          <option value="sensitive">sensitive</option>
          <option value="questionable">questionable</option>
          <option value="explicit">explicit</option>
        </select>
      </Field>
      <ErrorBox message={action.error ? errorText(action.error) : undefined} />
    </>
  );
}
