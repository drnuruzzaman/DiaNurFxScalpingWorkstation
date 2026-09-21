/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Override where the live websocket connects in dev, e.g. "127.0.0.1:9000". */
  readonly VITE_API_HOST?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
