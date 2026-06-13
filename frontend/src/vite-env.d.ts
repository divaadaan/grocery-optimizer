/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Backend origin for production builds; empty in dev (Vite proxy). */
  readonly VITE_API_BASE_URL?: string;
}
