# 開賣搶票模式

在訂票表單啟用模式後，依高鐵公告填寫台灣開賣日期與時間（預設午夜）。
快速期間為 1–5 分鐘、每輪失敗後等待為 3–10 秒，預設 2 分鐘／5 秒。
開賣時刻以 UTC 保存，普通任務的原售票判定與分鐘間隔不變。

系統最多並行兩筆任務。快速任務按到期時間公平排隊，首次嘗試優先於較晚到期的重試；
滿載時顯示「等待執行資源」，不增加 attempts。快速期間從指定時刻起算，不因排隊延長。
開賣前 30 秒保留所需空位，不中斷已在執行的工作；每個 worker 在服務啟動及重建時就載入
OCR 並完成本機推論，因此可提前完成預熱。伺服器需保持時間同步。

`Retry-After` 與 429 冷卻共享於 API 的所有訂票 worker；無有效 Retry-After 的 429 至少等待
30 秒。單輪 Session 重試最多四次，長等待交回排程，不截短官方等待時間。
最終確認送出前會同步保存確認標記。結果不明時任務暫停，須先向高鐵確認，再選擇未訂成繼續，
或輸入已訂成的 8 位 PNR。取消／刪除任務不等於取消高鐵訂位。

## API / CLI

`POST /schedule`、`PUT /tasks/{id}` 增加 `opening_mode`、`sales_open_at`（含時區）、
`burst_minutes`、`burst_retry_seconds`。建立與編輯要求開賣時刻在未來且不晚於乘車日結束。
舊 JSON 缺少欄位時預設關閉。偏好設定記住這些欄位，過期開賣時間在表單載入時清空。

`POST /tasks/{id}/resolve` 接受 `{"booked":false}` 或 `{"booked":true,"pnr":"12345678"}`，
僅擁有者可操作待確認任務；原本的 resume/edit 在待確認狀態回傳 409。
`POST /book` 現在也要求登入並透過同一個持久化 worker pool 執行一次，回應附 task_id。

CLI 排程可搭配：

```sh
--opening-mode --sales-open-at '2030-09-02T00:00:00+08:00' --burst-minutes 2 --burst-retry-seconds 5
```

## 驗證

後端：`python -m unittest discover -s tests`。其中 process 測試實際預熱兩個 OCR worker，
以模擬確認驗證 IPC、五筆派發與 PNR 保存，不送出真實訂位；可在 Docker `--network none` 執行。
Auth：在 auth image 執行 `python -m unittest discover -s /app -p test_booking_preferences.py`。
前端：`npm test`、`npm run build`。

瀏覽器測試：額外安裝 playwright-core，啟動 `npm run preview -- --port 4173`，
執行 `CHROMIUM_PATH=/path/to/chrome node tests/opening-browser.mjs`；所有 API 皆為 mock。

部署前備份 scheduler JSON 與 users.preferences，待在途 worker 完成後更新 API、Auth、Frontend。
回退前若存在 confirmation_pending／needs_confirmation，須先確認訂位結果；舊版本不認得此保護標記，
不可直接讓舊 scheduler 重新執行這些任務。
