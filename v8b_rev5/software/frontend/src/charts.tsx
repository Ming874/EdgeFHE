import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { number } from './data';
import type { Bundle, ModelEvidence } from './types';
export default function ComparisonChart({ m, throughput = false }: { m: ModelEvidence; throughput?: boolean }) {
  const data = [{name: throughput?'持續吞吐':'單筆 P50',CPU3:throughput?m.W2_cpu_records_s:m.W1_cpu_P50_ms,PL:throughput?m.W2_candidate_records_s:m.W1_candidate_P50_ms}];
  return <div className="chart" aria-label={throughput?'CPU與PL持續吞吐比較':'CPU與PL單筆延遲比較'}><ResponsiveContainer width="100%" height="100%">
    <BarChart data={data} barGap={15} margin={{top:12,right:12,left:0,bottom:0}}><CartesianGrid vertical={false} stroke="#e8eeea" strokeDasharray="3 5"/>
      <XAxis dataKey="name" axisLine={false} tickLine={false} tick={{fill:'#7d8981',fontSize:11}}/>
      <YAxis axisLine={false} tickLine={false} width={58} tick={{fill:'#7d8981',fontSize:11}} tickFormatter={v=>throughput?`${Math.round(v/1000)}k`:String(v)}/>
      <Tooltip formatter={(v,name)=>[number(Number(v),throughput?0:3)+(throughput?' 筆/s':' ms'),String(name)]} cursor={{fill:'#f5f8f5'}}/>
      <Bar dataKey="CPU3" fill="#c5d2ca" radius={[6,6,0,0]} maxBarSize={66} isAnimationActive={false}/>
      <Bar dataKey="PL" fill="#178365" radius={[6,6,0,0]} maxBarSize={66} isAnimationActive={false}/>
    </BarChart></ResponsiveContainer></div>;
}

export function SensorChart({ sessions }: { sessions: Bundle["sensors"]["sessions"] }) { return <div className="chart"><ResponsiveContainer width="100%" height="100%"><LineChart data={sessions} margin={{top:10,left:0,right:15,bottom:0}}><CartesianGrid strokeDasharray="3 5" stroke="#e6ece7"/><XAxis dataKey="index" tickLine={false} axisLine={false} fontSize={11}/><YAxis domain={['dataMin - 1','dataMax + 1']} tickLine={false} axisLine={false} width={45} tickFormatter={v=>`${number(v,0)}°`} fontSize={11}/><Tooltip labelFormatter={v=>`歷史 session ${v}`} formatter={v=>[`${number(Number(v),3)} °C`,'最高溫']}/><Line dataKey="temperatureC" type="monotone" stroke="#178365" strokeWidth={2} dot={{r:3,fill:'#178365'}} isAnimationActive={false}/></LineChart></ResponsiveContainer></div>; }

export function SoakChart({ series, names }: { series: Bundle['lan']['soak']['series']; names: Record<string,string> }) {
  const keys=Object.keys(names);const colors=['#178365','#d3a86f'];
  const data=series.map(x=>({hour:x.activeHour,[x.model]:x.recordsPerS}));
  return <div className="chart soak-chart" aria-label="六小時浸泡每窗口持續吞吐"><ResponsiveContainer width="100%" height="100%"><LineChart data={data} margin={{top:10,left:0,right:15,bottom:0}}>
    <CartesianGrid strokeDasharray="3 5" stroke="#e6ece7"/><XAxis dataKey="hour" type="number" domain={[0,'dataMax']} tickLine={false} axisLine={false} fontSize={11} tickFormatter={v=>`${number(v,0)} h`}/>
    <YAxis domain={[0,'dataMax + 20000']} tickLine={false} axisLine={false} width={50} fontSize={11} tickFormatter={v=>`${Math.round(v/1000)}k`}/>
    <Tooltip labelFormatter={v=>`有效時間 ${number(Number(v),2)} h`} formatter={(v,k)=>[`${number(Number(v))} 筆/s`,names[String(k)]||String(k)]}/>
    {keys.map((k,i)=><Line key={k} dataKey={k} name={names[k]} connectNulls dot={false} stroke={colors[i]} strokeWidth={1.6} isAnimationActive={false}/>)}
  </LineChart></ResponsiveContainer></div>;
}
