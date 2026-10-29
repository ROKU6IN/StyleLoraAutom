import { useEffect } from 'react';
import { queryClient } from '../state/queryClient';
import { useSession } from '../state/session';
import { subscribeEvents } from '../events/sseClient';
import { CoalescedInvalidation } from '../events/invalidate';
import { timing } from '../tokens/base';
export function useEvents(instance: string | undefined, pid: string | null) {
  useEffect(() => {
    if (!instance) return;
    const controller = new AbortController();
    const refresh = new CoalescedInvalidation(
      () => queryClient.invalidateQueries({ queryKey: ['project', pid] }),
      timing.invalidate,
    );
    void subscribeEvents(
      controller.signal,
      (frame, data) => {
        if (frame.event === 'config.changed' || frame.event === 'stream.resync_required') {
          void queryClient.invalidateQueries({ queryKey: ['product-config'] });
          refresh.request();
        }
        if (
          frame.event === 'storage.changed' ||
          frame.event === 'config.changed' ||
          frame.event === 'projects.changed' ||
          frame.event.startsWith('project.') ||
          frame.event === 'stream.resync_required'
        )
          void queryClient.invalidateQueries({ queryKey: ['storage-notices'] });
        if (
          frame.event === 'projects.changed' ||
          frame.event.startsWith('project.') ||
          frame.event === 'stream.resync_required'
        )
          void queryClient.invalidateQueries({ queryKey: ['recent-projects'] });
        if (frame.event.startsWith('prompt.') || frame.event === 'stream.resync_required')
          void queryClient.invalidateQueries({ queryKey: ['prompt'] });
        if (frame.event === 'llm.changed' || frame.event === 'stream.resync_required')
          void queryClient.invalidateQueries({ queryKey: ['llm'] });
        if (frame.event === 'presets.changed' || frame.event === 'stream.resync_required')
          void queryClient.invalidateQueries({ queryKey: ['presets'] });
        if (frame.event === 'backend.changed' || frame.event === 'stream.resync_required')
          void queryClient.invalidateQueries({ queryKey: ['backends'] });
        if (frame.event === 'catalog.changed' || frame.event === 'stream.resync_required') {
          void queryClient.invalidateQueries({ queryKey: ['catalog'] });
          void queryClient.invalidateQueries({ queryKey: ['rules'] });
        }
        if (frame.event === 'server.draining') useSession.getState().setConnection('draining');
        if (frame.event.startsWith('project.') || frame.event === 'stream.resync_required') {
          void queryClient.invalidateQueries({ queryKey: ['current'] });
          void queryClient.invalidateQueries({ queryKey: ['session'] });
          refresh.request();
        }
        if (data.project_id && data.project_id !== pid) return;
        if (/^(image|annotations|source|export|scheme|job)\./.test(frame.event)) refresh.request();
        if (frame.event === 'tagger.model_ready') {
          void queryClient.invalidateQueries({ queryKey: ['tagger-models'] });
          void queryClient.invalidateQueries({ queryKey: ['tagger-capabilities'] });
        }
        if (frame.event.startsWith('operation.'))
          void queryClient.invalidateQueries({ queryKey: ['operations'] });
      },
      (status) => {
        if (useSession.getState().connection === 'offline' && status === 'degraded') return;
        if (!['draining', 'reauth'].includes(useSession.getState().connection))
          useSession.getState().setConnection(status);
      },
    );
    return () => {
      controller.abort();
      refresh.close();
    };
  }, [instance, pid]);
}
