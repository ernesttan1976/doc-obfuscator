import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';

// The original HTML is kept as the visual/design source. React takes over its
// document after Vite has loaded this module.
document.querySelectorAll('body > *:not(#root)').forEach((element) => element.remove());

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
