'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

// Re-renders the server page on a timer, so re-running train.py or ckks_check.py shows up
// without a manual reload.
export default function AutoRefresh({ seconds = 10 }: { seconds?: number }) {
  const router = useRouter();
  const [enabled, setEnabled] = useState(true);
  const [stamp, setStamp] = useState('');

  useEffect(() => {
    setStamp(new Date().toLocaleTimeString('zh-TW'));
    if (!enabled) return;
    const timer = setInterval(() => {
      router.refresh();
      setStamp(new Date().toLocaleTimeString('zh-TW'));
    }, seconds * 1000);
    return () => clearInterval(timer);
  }, [enabled, seconds, router]);

  return (
    <div className="refresh">
      <label>
        <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
        每 {seconds} 秒自動更新
      </label>
      <span>上次更新 {stamp}</span>
    </div>
  );
}
