import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import {alphaTab} from '@coderline/alphatab-vite';
export default defineConfig({plugins:[react(),alphaTab()],server:{proxy:{'/api':'http://127.0.0.1:8787'}},build:{chunkSizeWarningLimit:1800}});
