/* UI-04: execute the shipped chart, not a copy of its tick formatter. */
'use strict';
const assert=require('assert');
const fs=require('fs');
const path=require('path');
const html=fs.readFileSync(path.join(__dirname,'..','dashboard.html'),'utf8');
function functionSource(name){
 const start=html.indexOf('function '+name+'(');
 assert.ok(start>=0,name+' exists');
 const open=html.indexOf('{',start);let depth=0;
 for(let i=open;i<html.length;i++){
  if(html[i]==='{')depth++;
  if(html[i]==='}'&&--depth===0)return html.slice(start,i+1);
 }
 throw new Error('unterminated '+name);
}
const source=['metricValue','seriesTickLabel','seriesTickPlan','drawSeries']
 .filter(name=>html.includes('function '+name+'(')).map(functionSource).join('\n');
let checks=0;
function check(name,value,detail){assert.ok(value,name+(detail?' -> '+detail:''));checks++;}
function localAt(y,m,d,h,minute){return new Date(y,m-1,d,h,minute||0,0,0).getTime()/1000;}
function makeSeries(start,count,step){
 return Array.from({length:count},(_,i)=>({at:start+i*step,total_tokens:i+1,completion_tokens:i+1,requests:1,errors:0,credit:0}));
}
function localDate(at){const d=new Date(at*1000);return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");}
function draw(series,step,width){
 const texts=[];
 const gradient={addColorStop(){}};
 const ctx=new Proxy({canvas:{id:'seriesCanvas'},measureText:t=>({width:String(t).length*6}),createLinearGradient:()=>gradient,
  fillText:(text,x,y)=>texts.push({text:String(text),x,y,width:String(text).length*6})},
  {get:(target,key)=>key in target?target[key]:(()=>{}),set:(target,key,value)=>{target[key]=value;return true;}});
 const canvas={parentElement:{clientWidth:width},style:{},getContext:()=>ctx,onmousemove:null};
 const elements={seriesLegend:{innerHTML:''}};
 const state={series:{series,bucket_seconds:step},metric:'tokens'};
 const fn=new Function('state','seriesCanvas','seriesTip','seriesColorHost','$','window','document',
  'getComputedStyle','fmtCompact','fmtDate','fmtTime','localDate',
  'let seriesLayout=null;const seriesColor=()=>"#888888";'+source+';return drawSeries;')(
   state,canvas,{style:{},innerHTML:''},null,id=>elements[id],
   {devicePixelRatio:1,innerWidth:1280},{body:{}},()=>({fontFamily:'sans-serif'}),
   v=>String(v),at=>{const d=new Date(at*1000);return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');},
   at=>new Date(at*1000).toLocaleString(),localDate);
 fn();
 return texts.filter(t=>t.y===154);
}
function assertReadable(labels,width){
 check('刻度文本不重复',new Set(labels.map(x=>x.text)).size===labels.length,JSON.stringify(labels));
 for(let i=1;i<labels.length;i++)check('相邻刻度不重叠',labels[i].x-labels[i].width/2>=labels[i-1].x+labels[i-1].width/2+7.99,JSON.stringify(labels));
 for(const label of labels){
  check('刻度左缘在绘图区内',label.x-label.width/2>=61.99,JSON.stringify(label));
  check('刻度右缘在绘图区内',label.x+label.width/2<=width-18+0.01,JSON.stringify(label));
 }
}
if(process.env.UI04_DST_CASE==='1'){
 const daily=draw(makeSeries(localAt(2026,11,1,0,0),3,86400),86400,1200);
 check('DST 日序列仅重复日期补真实时刻',daily.map(x=>x.text).join(',')==='11-01,11-01 23:00,11-02',JSON.stringify(daily));
 assertReadable(daily,1200);
 const labels=draw(makeSeries(Date.UTC(2026,10,1,5,0)/1000,4,3600),3600,1200);
 check('DST 回拨重复小时带日期消歧',labels.length===4&&labels[0].text==='01:00'&&labels[1].text==='11-01 01:00'&&labels[2].text==='02:00'&&labels[3].text==='03:00',JSON.stringify(labels));
 assertReadable(labels,1200);
 console.log('DST PASS='+checks+' FAIL=0');
}else{
const {spawnSync}=require('child_process');
const dst=spawnSync(process.execPath,[__filename],{env:{...process.env,UI04_DST_CASE:'1',TZ:'America/New_York'},encoding:'utf8'});
check('DST 子进程真实绘制通过',dst.status===0,dst.stdout+dst.stderr);
console.log(dst.stdout.trim());
const wrap=draw([localAt(2026,10,28,22,0),localAt(2026,10,29,0,0),localAt(2026,10,28,2,0)].map((at,i)=>({at,total_tokens:i,completion_tokens:i,requests:1,errors:0,credit:0})),3600,900);
check('首尾同日但中间跨日也带日期',wrap.every(x=>x.text.startsWith('10-')),JSON.stringify(wrap));
assertReadable(wrap,900);
const offset=draw(makeSeries(localAt(2026,10,28,4,30),4,3600),3600,500);
check('非零本地偏移不伪造整点',offset.map(x=>x.text).join(',')==='04:30,05:30,06:30,07:30',JSON.stringify(offset));
assertReadable(offset,500);
const tiny=draw(makeSeries(localAt(2026,10,28,23,30),6,1800),1800,120);
assertReadable(tiny,120);
const minute=makeSeries(localAt(2026,10,28,4,0),73,60);
const minuteLabels=draw(minute,60,500);
check('73 个分钟桶仍显示 6 个刻度',minuteLabels.length===6,JSON.stringify(minuteLabels));
check('分钟桶包含实际分钟',minuteLabels.every(x=>/^\d\d:\d\d$/.test(x.text)),JSON.stringify(minuteLabels));
assertReadable(minuteLabels,500);
const hourly=draw(makeSeries(localAt(2026,10,28,0,0),24,3600),3600,700);
check('小时桶保留 HH:00 形式',hourly.every(x=>/^\d\d:00$/.test(x.text)),JSON.stringify(hourly));
assertReadable(hourly,700);
const cross=makeSeries(localAt(2026,10,28,23,30),6,1800);
const crossLabels=draw(cross,1800,800);
check('跨日子日刻度带日期',crossLabels.every(x=>/^\d\d-\d\d \d\d:\d\d$/.test(x.text)),JSON.stringify(crossLabels));
assertReadable(crossLabels,800);
const daily=draw(makeSeries(localAt(2026,10,1,0,0),31,86400),86400,700);
check('普通天桶保留真实日期',daily.map(x=>x.text).join(',')==='10-01,10-07,10-13,10-19,10-25,10-31',JSON.stringify(daily));
assertReadable(daily,700);
const five=draw(makeSeries(localAt(2026,10,28,4,0),6,60),60,500);
check('5 分钟窗口端点正确',five[0].text==='04:00'&&five[five.length-1].text==='04:05',JSON.stringify(five));
assertReadable(five,500);
for(const width of [240,320,480,800]){
 const labels=draw(makeSeries(localAt(2026,10,28,22,0),73,3600),3600,width);
 check(width+'px 长标签自动减少数量',labels.length>=1&&labels.length<=6,JSON.stringify(labels));
 if(labels.length>1)check(width+'px 保留首尾时间',labels[0].text.includes('22:00')&&labels[labels.length-1].text.includes('22:00'),JSON.stringify(labels));
 else check(width+'px 极窄图退化为真实起始时间',labels[0].text.includes('22:00'),JSON.stringify(labels));
 assertReadable(labels,width);
}
check('空数据不画横轴',draw([],60,500).length===0);
const half=draw(makeSeries(localAt(2026,10,28,23,59),2,60),60,500);
check('跨午夜两点保留日期',half.length===2&&half[0].text==='10-28 23:59'&&half[1].text==='10-29 00:00',JSON.stringify(half));
assertReadable(half,500);
const two=draw(makeSeries(localAt(2026,10,28,4,7),2,60),60,240);
check('极窄两点仍保留首尾',two.length===2&&two[0].text==='04:07'&&two[1].text==='04:08',JSON.stringify(two));
assertReadable(two,240);
const crowded=draw(makeSeries(localAt(2026,10,28,4,7),3,60),60,160);
check('多桶极窄图保留首尾真实时间',crowded.length===2&&crowded[0].text==='04:07'&&crowded[1].text==='04:09',JSON.stringify(crowded));
const one=draw(makeSeries(localAt(2026,10,28,4,7),1,60),60,500);
check('单点显示真实分钟',one.length===1&&one[0].text==='04:07',JSON.stringify(one));
assertReadable(one,500);
console.log('PASS='+checks+' FAIL=0');
}
