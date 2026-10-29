import { create } from 'zustand';
import type { Session } from '../api/types';
export type Connection = 'booting' | 'online' | 'degraded' | 'offline' | 'reauth' | 'draining';
interface State {
  session: Session | null;
  connection: Connection;
  notice: string;
  setSession: (value: Session) => void;
  setConnection: (value: Connection) => void;
  notify: (value: string) => void;
}
export const useSession = create<State>((set) => ({
  session: null,
  connection: 'booting',
  notice: '',
  setSession: (session) => set({ session, connection: session.draining ? 'draining' : 'online' }),
  setConnection: (connection) => set({ connection }),
  notify: (notice) => set({ notice }),
}));
export const canWrite = () => ['online', 'degraded'].includes(useSession.getState().connection);
