import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { hydrate } from './lib/workspace'
import './styles/theme.css'
import './styles/app.css'

// Load the saved workspace BEFORE the first render: App reads its charts,
// indicators and layout from localStorage in useState initializers, so the
// server copy has to be there by then. hydrate() gives up after two seconds,
// so a slow or older API never stops the app starting.
hydrate().finally(() => {
  createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  )
})
