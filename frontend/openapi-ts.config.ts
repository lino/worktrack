/**
 * Configuration for `@hey-api/openapi-ts` code generation from `src/worktrack/web/openapi.json`.
 */
import { defineConfig } from '@hey-api/openapi-ts';

export default defineConfig({
  input: '../src/worktrack/web/openapi.json',
  output: 'src/api/generated',
  plugins: ['@hey-api/typescript'],
});
