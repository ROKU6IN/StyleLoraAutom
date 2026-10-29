import { useEffect, type ReactNode } from 'react';
import { usePrefs } from '../state/prefs';
export function ThemeProvider({ children }: { children: ReactNode }) {
  const theme = usePrefs((state) => state.theme),
    density = usePrefs((state) => state.density);
  useEffect(() => {
    const media = matchMedia('(prefers-color-scheme: dark)');
    const update = () => {
      document.documentElement.dataset.theme =
        theme === 'system' ? (media.matches ? 'dark' : 'light') : theme;
    };
    update();
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, [theme]);
  useEffect(() => {
    document.documentElement.dataset.density = density;
  }, [density]);
  return children;
}
