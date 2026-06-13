import {
  createContext,
  useCallback,
  useContext,
  useState,
  type ReactNode,
} from "react";
import type { UserResponse } from "../../api/types";

/**
 * The app has no auth; the "session" is whichever user profile was last
 * registered or loaded, persisted to localStorage.
 */

const STORAGE_KEY = "grocery-optimizer.user";

interface UserContextValue {
  user: UserResponse | null;
  setUser: (user: UserResponse) => void;
  clearUser: () => void;
}

const UserContext = createContext<UserContextValue | null>(null);

function readStoredUser(): UserResponse | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as UserResponse) : null;
  } catch {
    return null;
  }
}

export function UserProvider({ children }: { children: ReactNode }) {
  const [user, setUserState] = useState<UserResponse | null>(readStoredUser);

  const setUser = useCallback((next: UserResponse) => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    setUserState(next);
  }, []);

  const clearUser = useCallback(() => {
    localStorage.removeItem(STORAGE_KEY);
    setUserState(null);
  }, []);

  return (
    <UserContext.Provider value={{ user, setUser, clearUser }}>
      {children}
    </UserContext.Provider>
  );
}

export function useCurrentUser(): UserContextValue {
  const ctx = useContext(UserContext);
  if (!ctx) throw new Error("useCurrentUser must be used inside <UserProvider>");
  return ctx;
}
