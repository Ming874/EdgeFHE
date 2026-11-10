import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import { ArrowUpRight, Check, Cpu, Layers3, LockKeyhole, X } from 'lucide-react';

export function Badge({ children, tone = 'neutral' }: { children: ReactNode; tone?: string }) {
  return <span className={`badge ${tone}`}><i />{children}</span>;
}
export function Panel({ eyebrow, title, children, action, className = '' }: {
  eyebrow?: string; title: string; children: ReactNode; action?: ReactNode; className?: string;
}) {
  return <section className={`panel ${className}`}><header className="panel-heading"><div>
    {eyebrow && <p className="eyebrow">{eyebrow}</p>}<h2>{title}</h2></div>{action}</header>{children}</section>;
}
export function Metric({ label, value, unit, foot, accent = false }: { label: string; value: ReactNode; unit?: string; foot: string; accent?: boolean }) {
  return <div className={`metric-card ${accent ? 'accent' : ''}`}><div className="metric-label">{label}<ArrowUpRight size={15} /></div>
    <strong>{value}<small>{unit}</small></strong><p>{foot}</p></div>;
}
export function Modal({ title, children, close }: { title: string; children: ReactNode; close: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const d = ref.current; d?.showModal(); return () => d?.close(); }, []);
  return <dialog ref={ref} className="modal" aria-label={title} onCancel={e => { e.preventDefault(); close(); }}>
    <header><h2>{title}</h2><button onClick={close} className="icon-button" aria-label="關閉對話框"><X size={20} /></button></header>
    <div className="modal-body">{children}</div></dialog>;
}
export function BoardIllustration() {
  return <div className="board-art" aria-label="KV260 與密文運算的架構示意，非即時狀態">
    <svg viewBox="0 0 480 315" role="img" aria-labelledby="board-svg-title">
      <title id="board-svg-title">三個 RNS 分量、十二條 butterfly lanes 與密文資料路徑</title>
      <defs><linearGradient id="board" x1="0" x2="1" y1="0" y2="1"><stop stopColor="#1d524b"/><stop offset="1" stopColor="#102d2b"/></linearGradient>
        <pattern id="dots" width="19" height="19" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" fill="#c3d4ca"/></pattern></defs>
      <rect width="480" height="315" fill="url(#dots)" opacity=".5"/>
      <g fill="none" stroke="#b4cbc0" strokeWidth="1"><path d="M9 103h81v-40h85M26 218h92v29h92M264 35v35h122v49h61M272 275h106v-47h80"/><path d="M68 148h75M338 182h121" strokeDasharray="4 6"/></g>
      <g transform="translate(126 61)"><rect x="-12" y="10" width="228" height="192" rx="20" fill="#b8d8c9" opacity=".25"/>
        <rect width="210" height="180" rx="15" fill="url(#board)" stroke="#5d8c7d"/>
        <rect x="17" y="17" width="176" height="146" rx="8" fill="none" stroke="#4d8074"/>
        <g stroke="#b2d6c5" strokeWidth="3">{Array.from({length:8},(_,i)=><path key={i} d={`M${30+i*21} -8v8 M${30+i*21} 180v8 M-8 ${23+i*19}h8 M210 ${23+i*19}h8`}/>)}</g>
        <text x="30" y="46" fill="#cbe8d9" fontSize="12" letterSpacing="3">EDGEFHE</text>
        <text x="30" y="78" fill="white" fontSize="26" fontWeight="600">KV260</text>
        <text x="30" y="97" fill="#87b5a4" fontSize="10">RNS · NTT · CKKS</text>
        {Array.from({length:12},(_,i)=><rect key={i} x={30+(i%4)*33} y={112+Math.floor(i/4)*13} width="25" height="6" rx="2" fill={i<4?'#6be5b4':i<8?'#72b7b0':'#427d70'}/>)}</g>
      <g transform="translate(353 69)"><rect width="106" height="52" rx="10" fill="white" stroke="#d2ded5"/><text x="15" y="23" fill="#244a40" fontSize="11">CIPHERTEXT</text><text x="15" y="40" fill="#72847c" fontSize="10">c₀ · c₁</text></g>
      <g transform="translate(12 180)"><rect width="111" height="58" rx="11" fill="white" stroke="#d2ded5"/><text x="15" y="25" fill="#264d43" fontSize="11">OWNER ONLY</text><text x="15" y="43" fill="#72847c" fontSize="10">private input / key</text></g>
      <circle className="orbit-packet" cx="379" cy="228" r="5" fill="#21906e"/><circle cx="90" cy="103" r="4" fill="#8cb8a0"/>
      <text x="152" y="283" fill="#81938a" fontSize="10" letterSpacing="2">ARCHITECTURE · ILLUSTRATION</text>
    </svg><div className="board-label"><span><Cpu size={14}/>3 RNS × 4 lanes</span><span><LockKeyhole size={13}/>明確的信任邊界</span></div>
  </div>;
}
export function Empty({ children }: { children: ReactNode }) { return <div className="empty"><Layers3 size={30}/><p>{children}</p></div>; }
export function CheckItem({ children }: { children: ReactNode }) { return <div className="check-item"><Check size={16}/><span>{children}</span></div>; }
