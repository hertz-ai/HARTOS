import { ReactNode } from 'react';
import clsx from 'clsx';
import { TrendingUp, TrendingDown, Minus } from 'lucide-react';

interface StatCardProps {
  title: string;
  value: string | number;
  change?: number;
  changeLabel?: string;
  icon?: ReactNode;
  className?: string;
}

export default function StatCard({
  title,
  value,
  change,
  changeLabel,
  icon,
  className,
}: StatCardProps) {
  const getTrendIcon = () => {
    if (change === undefined || change === 0) {
      return <Minus className="w-4 h-4" />;
    }
    return change > 0 ? (
      <TrendingUp className="w-4 h-4" />
    ) : (
      <TrendingDown className="w-4 h-4" />
    );
  };

  const getTrendColor = () => {
    if (change === undefined || change === 0) return 'text-slate-500';
    return change > 0 ? 'text-green-500' : 'text-red-500';
  };

  return (
    <div
      className={clsx(
        'bg-white dark:bg-slate-800 rounded-xl p-6 shadow-sm border border-slate-200 dark:border-slate-700',
        className
      )}
    >
      <div className="flex items-start justify-between">
        <div>
          <p className="text-sm font-medium text-slate-500 dark:text-slate-400">{title}</p>
          <p className="text-2xl font-bold text-slate-900 dark:text-white mt-2">{value}</p>
          {change !== undefined && (
            <div className={clsx('flex items-center gap-1 mt-2', getTrendColor())}>
              {getTrendIcon()}
              <span className="text-sm font-medium">
                {change > 0 ? '+' : ''}
                {change}%
              </span>
              {changeLabel && (
                <span className="text-sm text-slate-500 dark:text-slate-400 ml-1">
                  {changeLabel}
                </span>
              )}
            </div>
          )}
        </div>
        {icon && (
          <div className="p-3 bg-primary-50 dark:bg-primary-900/20 rounded-lg">
            <div className="w-6 h-6 text-primary-500">{icon}</div>
          </div>
        )}
      </div>
    </div>
  );
}
