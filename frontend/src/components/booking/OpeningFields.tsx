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
      </div>
    </>}
  </section>;
}
