import { create } from 'zustand';
import { persist } from 'zustand/middleware';
export type Stage = 'checker' | 'white' | 'gray' | 'black' | 'dark';
export interface Preferences {
  theme: 'light' | 'dark' | 'system';
  density: 'compact' | 'comfortable';
  stage: Stage;
  shortcuts: boolean;
  autoAdvance: boolean;
}
const defaults: Preferences = {
  theme: 'light',
  density: 'compact',
  stage: 'checker',
  shortcuts: true,
  autoAdvance: true,
};
export function validPreferences(value: unknown): Preferences {
  const v = value && typeof value === 'object' ? (value as Partial<Preferences>) : {};
  return {
    theme: ['light', 'dark', 'system'].includes(v.theme ?? '') ? v.theme! : defaults.theme,
    density: v.density === 'comfortable' ? v.density : 'compact',
    stage: ['checker', 'white', 'gray', 'black', 'dark'].includes(v.stage ?? '') ? v.stage! : 'checker',
    shortcuts: typeof v.shortcuts === 'boolean' ? v.shortcuts : true,
    autoAdvance: typeof v.autoAdvance === 'boolean' ? v.autoAdvance : true,
  };
}
export const usePrefs = create<
  Preferences & { update: (value: Partial<Preferences>) => void; cycleStage: () => void }
>()(
  persist(
    (set, get) => ({
      ...defaults,
      update: (value) => set(validPreferences({ ...get(), ...value })),
      cycleStage: () => {
        const stages: Stage[] = ['checker', 'white', 'gray', 'black', 'dark'];
        set({ stage: stages[(stages.indexOf(get().stage) + 1) % stages.length] });
      },
    }),
    {
      name: 'stylelora.prefs.v1',
      version: 1,
      partialize: ({ theme, density, stage, shortcuts, autoAdvance }) => ({
        theme,
        density,
        stage,
        shortcuts,
        autoAdvance,
      }),
      merge: (persisted, current) => ({ ...current, ...validPreferences(persisted) }),
    },
  ),
);
