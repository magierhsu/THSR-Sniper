import { FieldErrors, UseFormRegister, UseFormWatch, UseFormSetValue } from 'react-hook-form';
import { BookingFormData } from '@/types';

export default function OpeningFields({ register, watch, errors, setValue }: {
  register: UseFormRegister<BookingFormData>;
  watch: UseFormWatch<BookingFormData>;
  errors: FieldErrors<BookingFormData>;
  setValue: UseFormSetValue<BookingFormData>;
}) {
  const enabled = watch('opening_mode');
  const value = watch('sales_open_at') || '';
  const preEntrySeconds = watch('pre_entry_seconds') || 0;
  const [date, time = '00:00'] = value.split('T');
  return <section className="p-4 border border-gray-700 rounded-lg space-y-4">
    <label className="flex items-center gap-3 text-text-primary">
      <input type="checkbox" {...register('opening_mode')} />開賣搶票模式
    </label>
    {enabled && <>
      <p className="text-sm text-text-muted">請依高鐵公告指定開賣時刻。系統最多同時執行 2 筆，多筆任務會公平排隊；快速期間結束後恢復一般間隔。</p>
      <div className="form-grid">
        <label className="form-group form-label">開賣日期與時間（台灣時間）
          <div className="flex flex-col sm:flex-row gap-2">
            <input aria-label="開賣日期（台灣時間）" type="date" className="rog-input min-w-0 w-full" value={date}
              onChange={e => setValue('sales_open_at', e.target.value ? `${e.target.value}T${time}` : '', {shouldValidate: true})} />
            <input aria-label="開賣時間（台灣時間）" type="time" className="rog-input min-w-0 w-full" value={time}
              onChange={e => setValue('sales_open_at', `${date}T${e.target.value || '00:00'}`, {shouldValidate: true})} />
          </div>
          <input type="hidden" {...register('sales_open_at', {
            validate: value => {
              if (!watch('opening_mode')) return true;
              const opening = value ? Date.parse(`${value}:00+08:00`) : NaN;
              const end = Date.parse(`${watch('date')}T23:59:59+08:00`);
              return (opening > Date.now() && opening <= end) || '請指定未來且不晚於乘車日的開賣時間';
            },
          })} />
          {errors.sales_open_at && <span className="text-rog-danger text-sm">{errors.sales_open_at.message}</span>}
        </label>
        <label className="form-group form-label">快速重試期間（分鐘）
          <select className="rog-select" {...register('burst_minutes', {valueAsNumber: true})}>
            {[1,2,3,4,5].map(n => <option key={n} value={n}>{n} 分鐘</option>)}
          </select>
        </label>
        <label className="form-group form-label">每輪失敗後等待（秒）
          <select className="rog-select" {...register('burst_retry_seconds', {valueAsNumber: true})}>
            {[3,4,5,6,7,8,9,10].map(n => <option key={n} value={n}>{n} 秒</option>)}
          </select>
        </label>
        <label className="form-group form-label">
          提前建立 Session（測試）
          <select
            aria-label="提前建立 Session（測試）"
            className="rog-select"
            {...register('pre_entry_seconds', {
              valueAsNumber: true,
              validate: value => [0, 30, 60].includes(Number(value)) || '只能選擇關閉、30 或 60 秒',
            })}
          >
            <option value={0}>關閉（開賣時建立）</option>
            <option value={30}>提前 30 秒（只觀察 Session）</option>
            <option value={60}>提前 60 秒（只觀察 Session）</option>
          </select>
          {preEntrySeconds > 0 && (
            <span className="text-text-muted text-xs mt-1 block">
              只觀察 Session；不會送出查詢、選車或訂票請求。正式訂票會在開賣時另外建立自己的 Session；測試時請勿占滿兩個共用 worker。
            </span>
          )}
        </label>
      </div>
    </>}
  </section>;
}
