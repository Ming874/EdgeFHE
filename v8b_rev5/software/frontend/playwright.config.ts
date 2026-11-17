import { defineConfig } from '@playwright/test';
const python = process.env.SHOWCASE_PYTHON || 'python';
export default defineConfig({
  testDir:'./tests',workers:1,fullyParallel:false,timeout:30000,
  reporter:[['list'],['json',{outputFile:'../qa/playwright-results.json'}]],
  use:{baseURL:'http://127.0.0.1:5188',trace:'retain-on-failure',reducedMotion:'reduce'},
  webServer:{command:`"${python}" -X utf8 -B ../tools/serve.py --root dist --port 5188 --quiet`,
    url:'http://127.0.0.1:5188/health',reuseExistingServer:false,timeout:30000},
  projects:[
    {name:'desktop-1366',use:{browserName:'chromium',viewport:{width:1366,height:768}}},
    {name:'presentation-1920',use:{browserName:'chromium',viewport:{width:1920,height:1080}}},
    {name:'mobile-390',use:{browserName:'chromium',viewport:{width:390,height:844}}},
  ],
});
