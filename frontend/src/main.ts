/**
 * Browser entrypoint mounting the Vue 3 + Vuetify + Pinia + `pinia-orm` Worktrack cockpit.
 */
import { createPinia } from 'pinia';
import { createORM } from 'pinia-orm';
import { createApp } from 'vue';
import { createVuetify } from 'vuetify';
import App from './App.vue';
import './styles/cockpit.css';

/**
 * Bootstrap and mount the Worktrack browser cockpit onto `#app`.
 */
export function bootstrapCockpitApp(): void {
  const pinia = createPinia().use(createORM());
  const vuetify = createVuetify();
  const app = createApp(App);
  app.use(pinia);
  app.use(vuetify);
  app.mount('#app');
}

if (typeof document !== 'undefined' && document.getElementById('app')) {
  bootstrapCockpitApp();
}
