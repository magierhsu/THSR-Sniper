// Run after npm run build, with vite preview on :4173 and playwright-core installed.
import { chromium } from 'playwright-core';
import assert from 'node:assert/strict';

const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH, headless: true, args:['--no-sandbox']});
try {
  for (const viewport of [{width:1440,height:1000},{width:390,height:844}]) {
    const context = await browser.newContext({viewport});
    const token = `e30.${Buffer.from(JSON.stringify({exp:4102444800})).toString('base64url')}.test`;
    await context.addInitScript(token => {
      localStorage.setItem('auth_token', token);
      localStorage.setItem('auth-storage', JSON.stringify({state:{isAuthenticated:true,token,user:{id:1,username:'test'},refreshToken:'test'},version:0}));
    }, token);
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    const prefs = {version:1,from_station:1,to_station:2,date:'2030-09-20',time:1,
      adult_cnt:1,child_cnt:2,preferred_train_numbers:['825','838'],interval_minutes:3,
      opening_mode:true,sales_open_at:'2030-09-01T16:00:00Z',burst_minutes:4,burst_retry_seconds:8};
    const task = {id:'browser-test',status:'paused',from_station:1,to_station:2,date:'2030/09/20',
      time:1,adult_cnt:1,child_cnt:2,interval_minutes:3,attempts:0,created_at:'2026-09-01T00:00:00Z',
      ...prefs,needs_confirmation:false,execution_phase:'paused',same_opening_tasks:5,concurrency_limit:2};
    let saved, scheduled, remembered;
    await page.route(/\/(api|auth)\//, async route => {
      const path = new URL(route.request().url()).pathname;
      const method = route.request().method();
      let body = {};
      if (path.endsWith('/stations')) body=[{id:1,name:'南港'},{id:2,name:'台北'}];
      else if (path.endsWith('/times')) body=[{id:1,time:'600A',formatted_time:'06:00'}];
      else if (path.endsWith('/thsr-info')) body={personal_id:'A123456789',use_membership:false};
      else if (path.endsWith('/booking-preferences')) {
        if (method==='PUT') remembered=route.request().postDataJSON();
        body={preferences:prefs};
      } else if (path.endsWith('/me')) body={id:1,username:'test',email:'test@example.invalid'};
      else if (path.endsWith('/schedule')) {scheduled=route.request().postDataJSON();body={success:true,task_id:task.id};}
      else if (path.endsWith('/results')) body={success:true,results:[task],total:1,limit:50,offset:0};
      else if (path.endsWith('/tasks/browser-test') && method==='PUT') {saved=route.request().postDataJSON();body=task;}
      await route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
    });
    await page.goto('http://127.0.0.1:4173/booking');
    await page.getByLabel('開賣日期（台灣時間）').waitFor();
    assert.equal(await page.getByLabel('開賣日期（台灣時間）').inputValue(), '2030-09-02');
    assert.equal(await page.getByLabel('開賣時間（台灣時間）').inputValue(), '00:00');
    assert.equal(await page.locator('[name=burst_minutes]').inputValue(), '4');
    await page.screenshot({path:`/tmp/thsr-opening-form-${viewport.width}.png`,fullPage:true});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.locator('button[type=submit]').click();
    await page.waitForURL('**/tasks');
    assert.equal(scheduled.sales_open_at, '2030-09-01T16:00:00.000Z');
    assert.equal(remembered.burst_retry_seconds, 8);
    await page.getByRole('button',{name:'修改內容',exact:true}).click();
    const dialog=page.getByRole('dialog');
    await dialog.waitFor();
    await dialog.locator('[name=burst_minutes]').selectOption('5');
    await dialog.getByLabel('開賣時間（台灣時間）').fill('00:01');
    await page.screenshot({path:`/tmp/thsr-opening-edit-${viewport.width}.png`,fullPage:true});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await dialog.getByRole('button',{name:'儲存修改'}).click();
    await dialog.waitFor({state:'hidden'});
    assert.equal(saved.burst_minutes, 5);
    assert.equal(saved.sales_open_at, '2030-09-01T16:01:00.000Z');
    task.needs_confirmation=true;
    task.execution_phase='needs_confirmation';
    await page.getByRole('button',{name:'刷新'}).click();
    await page.getByRole('button',{name:'確認未訂成，繼續'}).waitFor();
    assert.equal(await page.getByRole('button',{name:'修改內容',exact:true}).count(),0);
    await page.screenshot({path:`/tmp/thsr-opening-review-${viewport.width}.png`,fullPage:true});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors,[]);
    console.log('PASS opening form/edit/review',viewport.width);
    await context.close();
  }
} finally { await browser.close(); }
