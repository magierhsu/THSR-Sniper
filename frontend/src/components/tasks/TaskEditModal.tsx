import OpeningFields from '@/components/booking/OpeningFields';
import { openingLocal, openingPayload } from '@/utils/opening';
import React from 'react';
import { XMarkIcon } from '@heroicons/react/24/outline';
import { useForm } from 'react-hook-form';
import { useMutation, useQueryClient } from 'react-query';
import { toast } from 'react-toastify';
import { thsrApi } from '@/services/api';
import {
  BookingFormData,
  BookingTask,
  StationInfo,
  THSRInfo,
  TaskUpdateRequest,
  TimeSlotInfo,
} from '@/types';
import LoadingSpinner from '@/components/ui/LoadingSpinner';
import {
  DEPARTURE_TIME_RANGE_OPTIONS,
  formatDepartureTimeRange,
} from '@/utils/timeRange';
import {
  formatPreferredTrainNumbers,
  parsePreferredTrainNumbers,
} from '@/utils/trainPreferences';

interface TaskEditModalProps {
  task: BookingTask;
  stations: StationInfo[];
  timeSlots: TimeSlotInfo[];
  thsrInfo: THSRInfo | undefined;
  onClose: () => void;
}

const TaskEditModal: React.FC<TaskEditModalProps> = ({
  task,
  stations,
  timeSlots,
  thsrInfo,
  onClose,
}) => {
  const queryClient = useQueryClient();
  const {
    register,
    setValue,
    handleSubmit,
    watch,
    formState: { errors },
  } = useForm<BookingFormData>({
    defaultValues: {
      opening_mode: task.opening_mode || false,
      sales_open_at: openingLocal(task.sales_open_at),
      burst_minutes: task.burst_minutes || 2,
      burst_retry_seconds: task.burst_retry_seconds || 5,
      fromStation: task.from_station,
      toStation: task.to_station,
      date: task.date.replace(/\//g, '-'),
      adultCount: task.adult_cnt || 0,
      studentCount: task.student_cnt || 0,
      childCount: task.child_cnt || 0,
      seniorCount: task.senior_cnt || 0,
      disabledCount: task.disabled_cnt || 0,
      departureTime: task.time,
      departureTimeRangeMinutes: task.time_range_minutes || 30,
      preferredTrainNumbers: formatPreferredTrainNumbers(
        task.preferred_train_numbers
      ),
      seatPreference: task.seat_prefer || 0,
      classType: task.class_type || 0,
      useOCR: !task.no_ocr,
      intervalMinutes: task.interval_minutes,
      maxAttempts: task.max_attempts,
    },
    mode: 'onChange',
  });

  const fromStation = watch('fromStation');

  const updateMutation = useMutation(
    (payload: TaskUpdateRequest) => thsrApi.updateTask(task.id, payload),
    {
      onSuccess: () => {
        toast.success('任務內容已更新，仍維持暫停');
        queryClient.invalidateQueries(['tasks']);
        queryClient.invalidateQueries(['resultsStats']);
        onClose();
      },
      onError: (error: Error) => {
        toast.error(`修改失敗：${error.message}`);
      },
    }
  );

  const onSubmit = (data: BookingFormData) => {
    if (!thsrInfo?.personal_id) {
      toast.error('請先在個人設定中設定身分證字號');
      return;
    }

    const preferred = parsePreferredTrainNumbers(data.preferredTrainNumbers);
    if (preferred.error) {
      toast.error(preferred.error);
      return;
    }

    const counts = [
      data.adultCount,
      data.studentCount,
      data.childCount,
      data.seniorCount,
      data.disabledCount,
    ].map(value => Number(value) || 0);
    const total = counts.reduce((sum, value) => sum + value, 0);
    if (total < 1 || total > 10) {
      toast.error('總票數必須介於 1 到 10 張');
      return;
    }

    updateMutation.mutate({
      from_station: Number(data.fromStation),
      to_station: Number(data.toStation),
      date: data.date.replace(/-/g, '/'),
      personal_id: thsrInfo.personal_id,
      use_membership: thsrInfo.use_membership,
      adult_cnt: counts[0],
      student_cnt: counts[1],
      child_cnt: counts[2],
      senior_cnt: counts[3],
      disabled_cnt: counts[4],
      time: Number(data.departureTime),
      time_range_minutes: Number(data.departureTimeRangeMinutes),
      preferred_train_numbers: preferred.values,
      seat_prefer: Number(data.seatPreference),
      class_type: Number(data.classType),
      no_ocr: !data.useOCR,
      ...openingPayload(data),
      interval_minutes: Number(data.intervalMinutes),
      max_attempts: data.maxAttempts ? Number(data.maxAttempts) : undefined,
    });
  };

  const ticketFields = [
    ['adultCount', '成人票'],
    ['studentCount', '學生票'],
    ['childCount', '兒童票'],
    ['seniorCount', '敬老票'],
    ['disabledCount', '愛心票'],
  ] as const;

  return (
    <div className="modal-overlay p-4" role="dialog" aria-modal="true" aria-labelledby="edit-task-title">
      <form
        onSubmit={handleSubmit(onSubmit)}
        className="w-full max-w-4xl max-h-[calc(100vh-2rem)] overflow-y-auto bg-bg-card border border-gray-700 rounded-lg shadow-2xl"
      >
        <div className="sticky top-0 z-10 flex items-center justify-between px-5 py-4 bg-bg-card border-b border-gray-700">
          <div className="min-w-0">
            <h2 id="edit-task-title" className="text-lg font-bold text-text-primary">修改任務</h2>
            <p className="text-sm text-text-muted truncate">#{task.id.slice(-8)}</p>
          </div>
          <button type="button" onClick={onClose} className="p-2 text-text-muted hover:text-text-primary" title="關閉" aria-label="關閉">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>

        <div className="p-5 space-y-6">
          <OpeningFields register={register} watch={watch} errors={errors} setValue={setValue} />
          <section>
            <h3 className="text-base font-semibold text-text-primary mb-3">行程資訊</h3>
            <div className="form-grid">
              <div className="form-group">
                <label className="form-label" htmlFor="edit-from">出發站</label>
                <select {...register('fromStation', { required: true })} id="edit-from" className="rog-select">
                  {stations.map(station => <option key={station.id} value={station.id}>{station.name}</option>)}
                </select>
              </div>
              <div className="form-group">
                <label className="form-label" htmlFor="edit-to">到達站</label>
                <select
                  {...register('toStation', {
                    required: true,
                    validate: value => value !== fromStation || '出發站和到達站不能相同',
                  })}
                  id="edit-to"
                  className="rog-select"
                >
                  {stations.map(station => <option key={station.id} value={station.id}>{station.name}</option>)}
                </select>
                {errors.toStation && <p className="text-rog-danger text-sm">{errors.toStation.message}</p>}
              </div>
              <div className="form-group">
                <label className="form-label" htmlFor="edit-date">出發日期</label>
                <input {...register('date', { required: '請選擇日期' })} id="edit-date" type="date" className="rog-input" />
              </div>
              <div className="form-group">
                <label className="form-label" htmlFor="edit-time">出發時間</label>
                <select {...register('departureTime', { required: '請選擇時間' })} id="edit-time" className="rog-select">
                  {timeSlots.map(slot => <option key={slot.id} value={slot.id}>{slot.formatted_time} ({slot.time})</option>)}
                </select>
              </div>
              <div className="form-group">
                <label className="form-label" htmlFor="edit-range">可接受出發範圍</label>
                <select {...register('departureTimeRangeMinutes', { required: true })} id="edit-range" className="rog-select">
                  {DEPARTURE_TIME_RANGE_OPTIONS.map(minutes => <option key={minutes} value={minutes}>{formatDepartureTimeRange(minutes)}</option>)}
                </select>
              </div>
              <div className="form-group">
                <label className="form-label" htmlFor="edit-preferred">偏好車次（選填）</label>
                <input
                  {...register('preferredTrainNumbers', {
                    validate: value => parsePreferredTrainNumbers(value).error || true,
                  })}
                  id="edit-preferred"
                  className="rog-input"
                  placeholder="825, 838, 1320"
                />
                {errors.preferredTrainNumbers && <p className="text-rog-danger text-sm">{errors.preferredTrainNumbers.message}</p>}
              </div>
            </div>
          </section>

          <section>
            <h3 className="text-base font-semibold text-text-primary mb-3">乘客資訊</h3>
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
              {ticketFields.map(([fieldName, label]) => (
                <div className="form-group" key={fieldName}>
                  <label className="form-label" htmlFor={`edit-${fieldName}`}>{label}</label>
                  <input
                    {...register(fieldName, { min: 0, max: 10 })}
                    id={`edit-${fieldName}`}
                    type="number"
                    min="0"
                    max="10"
                    className="rog-input"
                  />
                </div>
              ))}
            </div>
          </section>

          <section>
            <h3 className="text-base font-semibold text-text-primary mb-3">偏好與排程</h3>
            <div className="form-grid">
              <div className="form-group">
                <label className="form-label" htmlFor="edit-seat">座位偏好</label>
                <select {...register('seatPreference')} id="edit-seat" className="rog-select">
                  <option value={0}>不指定</option><option value={1}>靠窗</option><option value={2}>靠走道</option>
                </select>
              </div>
              <div className="form-group">
                <label className="form-label" htmlFor="edit-class">車廂類型</label>
                <select {...register('classType')} id="edit-class" className="rog-select">
                  <option value={0}>標準車廂</option><option value={1}>商務車廂</option>
                </select>
              </div>
              <div className="form-group">
                <label className="form-label" htmlFor="edit-interval">重試間隔（分鐘）</label>
                <input {...register('intervalMinutes', { required: true, min: 1, max: 60 })} id="edit-interval" type="number" min="1" max="60" className="rog-input" />
              </div>
              <div className="form-group">
                <label className="form-label" htmlFor="edit-attempts">最大嘗試次數（選填）</label>
                <input {...register('maxAttempts', { min: 1 })} id="edit-attempts" type="number" min="1" className="rog-input" placeholder="不限制" />
              </div>
              <label className="flex items-center gap-3 text-text-secondary cursor-pointer">
                <input {...register('useOCR')} type="checkbox" className="w-4 h-4 accent-rog-primary" />
                啟用 OCR 驗證碼辨識
              </label>
            </div>
          </section>
        </div>

        <div className="sticky bottom-0 flex flex-col-reverse sm:flex-row sm:justify-end gap-3 px-5 py-4 bg-bg-card border-t border-gray-700">
          <button type="button" onClick={onClose} className="rog-btn rog-btn-secondary">取消</button>
          <button type="submit" disabled={updateMutation.isLoading || !thsrInfo?.personal_id} className="rog-btn rog-btn-primary min-w-32">
            {updateMutation.isLoading ? <LoadingSpinner size="small" /> : '儲存修改'}
          </button>
        </div>
      </form>
    </div>
  );
};

export default TaskEditModal;
