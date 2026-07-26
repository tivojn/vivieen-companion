/* Headless probe: runs web/index.html's real render loop against a stub
   canvas, then recovers what was actually PAINTED from the drawImage
   source rects + the composed matrix. Landmark re-detection and eyeball
   estimates are too noisy; the draw calls are ground truth. */
const fs=require('fs'),path=require('path'),vm=require('vm');
const ROOT=process.argv[2];
if(!ROOT){console.error('usage: node qa/life_probe.js <test-root> [label]');process.exit(2);}
const html=fs.readFileSync(path.join(ROOT,'web/index.html'),'utf8');
const SRC=html.slice(html.indexOf('<script>')+8, html.lastIndexOf('</script>'));
let ASSET_DIR=path.join(ROOT,'web/assets');
try{
  const active=JSON.parse(fs.readFileSync(path.join(ROOT,'active.json'),'utf8')).slug;
  const runtime=path.join(ROOT,'avatars',active,'runtime');
  if(fs.existsSync(path.join(runtime,'manifest.json')))ASSET_DIR=runtime;
}catch{}
const MAN=JSON.parse(fs.readFileSync(path.join(ASSET_DIR,'manifest.json'),'utf8'));

const mul=(A,B)=>[A[0]*B[0]+A[2]*B[1],A[1]*B[0]+A[3]*B[1],A[0]*B[2]+A[2]*B[3],
                  A[1]*B[2]+A[3]*B[3],A[0]*B[4]+A[2]*B[5]+A[4],A[1]*B[4]+A[3]*B[5]+A[5]];
const ap=(M,p)=>[M[0]*p[0]+M[2]*p[1]+M[4], M[1]*p[0]+M[3]*p[1]+M[5]];
function inv(M){const D=M[0]*M[3]-M[1]*M[2];const a=M[3]/D,b=-M[1]/D,c=-M[2]/D,d=M[0]/D;
  return [a,b,c,d,-(a*M[4]+c*M[5]),-(b*M[4]+d*M[5])];}

class Ctx{
  constructor(){this.m=[1,0,0,1,0,0];this.st=[];this.draws=[];this.ops=[];this.globalAlpha=1;this.fillStyle='';}
  setTransform(a,b,c,d,e,f){this.m=[a,b,c,d,e,f];this.ops.push(this.m.slice());}
  transform(a,b,c,d,e,f){this.m=mul(this.m,[a,b,c,d,e,f]);}
  translate(x,y){this.m=mul(this.m,[1,0,0,1,x,y]);}
  rotate(t){this.m=mul(this.m,[Math.cos(t),Math.sin(t),-Math.sin(t),Math.cos(t),0,0]);}
  scale(x,y){this.m=mul(this.m,[x,0,0,y,0,0]);}
  save(){this.st.push(this.m.slice());}
  restore(){this.m=this.st.pop()||[1,0,0,1,0,0];}
  fillRect(){} clearRect(){} beginPath(){} fill(){}
  drawImage(img,...a){
    let s,d;
    if(a.length===2){s=[0,0,img.width,img.height];d=[a[0],a[1],img.width,img.height];}
    else if(a.length===4){s=[0,0,img.width,img.height];d=a.slice(0,4);}
    else {s=a.slice(0,4);d=a.slice(4,8);}
    this.draws.push({n:img._n,m:this.m.slice(),s,d,al:this.globalAlpha});
  }
}
const CTX=new Ctx();

const DIMS={};
for(const f of fs.readdirSync(ASSET_DIR)){
  if(f.endsWith('.png')){const b=fs.readFileSync(path.join(ASSET_DIR,f));
    DIMS[f]=[b.readUInt32BE(16),b.readUInt32BE(20)];}
}
class Img{
  constructor(){this._n='';this.width=MAN.w;this.height=MAN.h;}
  set src(v){this._n=String(v).split('/').pop().split(/[?#]/)[0];
    const d=DIMS[this._n]||[MAN.w,MAN.h];this.width=d[0];this.height=d[1];
    queueMicrotask(()=>{if(this.onload)this.onload();});}
  get src(){return this._n;}
}
const El=()=>({textContent:'',className:'',disabled:false,value:'',style:{},
  classList:{add(){},remove(){}},addEventListener(){},getContext:()=>CTX,width:0,height:0});
const els={};
const doc={getElementById:id=>(els[id]=els[id]||El()),
           activeElement:null,addEventListener(){}};

let NOW=0,rafcb=null;
let seed=987654321>>>0;
const rnd=()=>{seed=(seed*1664525+1013904223)>>>0;return seed/4294967296;};

function envRMS(t){const s=t/1000;
  let e=0.105+0.075*Math.sin(2*Math.PI*4.6*s)+0.035*Math.sin(2*Math.PI*2.1*s+1.1);
  const g=Math.sin(2*Math.PI*0.23*s);           // phrase-level pauses
  e*= g>-0.55?1:0.12;
  return Math.max(0.008,e);}
const AN={
  getByteTimeDomainData(a){const amp=Math.min(1,envRMS(NOW)*Math.SQRT2);
    for(let i=0;i<a.length;i++){const v=128+127*amp*Math.sin(i*0.37+NOW*0.011);
      a[i]=v<0?0:v>255?255:v|0;}},
  getByteFrequencyData(a){a.fill(70);}
};
const ACTX={get currentTime(){return NOW/1000;},sampleRate:24000};

const G={console,Math:undefined,setTimeout,clearTimeout,queueMicrotask,
  performance:{now:()=>NOW},
  requestAnimationFrame:cb=>{rafcb=cb;},
  devicePixelRatio:2,innerWidth:1280,innerHeight:800,
  addEventListener(){},removeEventListener(){},
  document:doc,Image:Img,navigator:{},atob:s=>s,
  fetch:async u=>({json:async()=>{
    if(String(u).includes('manifest'))return JSON.parse(JSON.stringify(MAN));
    if(String(u).includes('health'))return {warm:true,ollama:true};
    return {};}}),
};
delete G.Math;
const EPI=`;const __blinkStarts=[];const __planBlink=planBlink;
let __lastHead=null,__lastHeadDrive=0;const __headAt=headAt;
headAt=(...args)=>{__lastHeadDrive=Number(args[1]||0);return (__lastHead=__headAt(...args));};
planBlink=now=>{const before=blinks.length;__planBlink(now);
  for(let i=before;i<blinks.length;i++)__blinkStarts.push(blinks[i].t0);};
globalThis.__P={loop:t=>loop(t),
  setSpeaking:v=>{speaking=v;},setAudio:(a,n)=>{actx=a;analyser=n;},
  setTrack:(t,t0,d)=>{track=stabiliseTrack(t,d);tIdx=0;audioT0=t0;},
  getBlinkStarts:()=>__blinkStarts.slice(),
  get minPose(){return MIN_POSE;},get xfade(){return XFADE;},
  get lvl(){return lvl;},get cheek(){return cheek;},get curV(){return curV;},
  get head(){return __lastHead;},get headDrive(){return __lastHeadDrive;},
  get browEnv(){return browEnv;},get ready(){return ready;}};`;

G.__rnd=rnd;
const ctxObj=vm.createContext(G);
G.globalThis=ctxObj; G.window=ctxObj; G.self=ctxObj;
vm.runInContext('Math.random=__rnd;',ctxObj);
vm.runInContext(SRC+EPI,ctxObj);
const P=G.__P;

function bases(){return CTX.draws.filter(d=>/_(open|shut)\.(jpg|png)$/.test(d.n||''));}
function bandOverlap(){
  const bs=bases();if(!bs.length)return null;
  const name=bs[bs.length-1].n,group=[];
  for(let i=bs.length-1;i>=0&&bs[i].n===name;i--)group.push(bs[i]);
  group.sort((a,b)=>a.s[1]-b.s[1]);
  if(group.length<2)return null;
  let overlap=Infinity;
  for(let i=1;i<group.length;i++)
    overlap=Math.min(overlap,group[i-1].s[1]+group[i-1].s[3]-group[i].s[1]);
  return overlap;
}
function Smat(){ // keyframe->canvas fit, recovered from the ops log
  for(let i=CTX.ops.length-1;i>=0;i--){const o=CTX.ops[i];
    if(o[1]===0&&o[2]===0&&Math.abs(o[0]-o[3])<1e-9&&o[0]!==1)return o;}
  return null;}
function warpPt(p){ // keyframe px -> keyframe px, head warp only (fit removed)
  const S=Smat(); if(!S)return null;
  const bs=bases(); let hit=null;
  for(const d of bs){ if(p[1]>=d.s[1]&&p[1]<d.s[1]+d.s[3]) hit=d; }
  if(!hit)return null;
  const q=[hit.d[0]+(p[0]-hit.s[0])*hit.d[2]/hit.s[2],
           hit.d[1]+(p[1]-hit.s[1])*hit.d[3]/hit.s[3]];
  return ap(inv(S),ap(hit.m,q));
}
function snapIdx(name,eh){
  for(let i=CTX.draws.length-1;i>=0;i--){const d=CTX.draws[i];
    if(d.n===name)return Math.round(d.s[1]/eh);}
  return null;}

const CX=MAN.neck.x, LM={crown:122,eyes:MAN.neck.ref,chin:800,collar:1000};
function stats(a){const n=a.length,mu=a.reduce((x,y)=>x+y,0)/n;
  let v=0;for(const x of a)v+=(x-mu)*(x-mu);
  return {mean:mu,sd:Math.sqrt(v/n),min:Math.min(...a),max:Math.max(...a)};}
function longestRun(a){let best=0,run=0;
  for(const value of a){run=value?run+1:0;if(run>best)best=run;}return best;}

async function run(label,secs,speak){
  const blinkFrom=P.getBlinkStarts().length;
  P.setSpeaking(!!speak);
  if(speak){P.setAudio(ACTX,AN);
    const vs=['sil','aa','E','ih','oh','ou','SS','nn','DD','kk','CH','RR','TH','FF','PP'];
    const tr=[];for(let i=0,t=0;i<secs*14;i++,t+=0.045+rnd()*0.08)
      tr.push([t,vs[(i*5+3)%vs.length]]);
    P.setTrack(tr,NOW/1000,secs);}
  const rec={hh:[],crown:[],eyes:[],chin:[],collar:[],ck:[],bw:[],ckv:[],lid:[],
             headY:[],headDrive:[],viseme:[],bandOverlap:[]};
  const N=Math.round(secs*60);
  for(let i=0;i<N;i++){
    NOW+=1000/60;
    CTX.draws=[];CTX.ops=[];
    const cb=rafcb;rafcb=null;cb(NOW);
    const overlap=bandOverlap();if(overlap!==null)rec.bandOverlap.push(overlap);
    const c=warpPt([CX,LM.crown]),e=warpPt([CX,LM.eyes]),
          ch=warpPt([CX,LM.chin]),co=warpPt([CX,LM.collar]);
    if(!c||!ch)continue;
    rec.hh.push(ch[1]-c[1]);
    rec.crown.push(c[1]-LM.crown);rec.eyes.push(e[1]-LM.eyes);
    rec.chin.push(ch[1]-LM.chin);rec.collar.push(co[1]-LM.collar);
    const ci=snapIdx('cheek_l.png',MAN.cheek.l.box[3]);
    const bi=snapIdx('brow_l.png',MAN.brow.l.box[3]);
    const li=snapIdx('eye_l.png',MAN.eyes.l.box[3]);
    if(ci!==null)rec.ck.push(ci); if(bi!==null)rec.bw.push(bi);
    rec.lid.push(li===null?0:1);
    rec.ckv.push(P.cheek);
    rec.headY.push(P.head.y);rec.headDrive.push(Math.abs(P.headDrive));
    rec.viseme.push(P.curV);
  }
  const H0=LM.chin-LM.crown, hs=stats(rec.hh);
  const chg=a=>{let n=0;for(let i=1;i<a.length;i++)if(a[i]!==a[i-1])n++;return n/(a.length/60);};
  const blinkStarts=P.getBlinkStarts().slice(blinkFrom);
  const blinkGaps=blinkStarts.slice(1).map((value,index)=>value-blinkStarts[index]);
  const switchJump=[],steadyJump=[];
  for(let i=1;i<rec.headY.length;i++){
    const jump=Math.abs(rec.headY[i]-rec.headY[i-1]);
    (rec.viseme[i]===rec.viseme[i-1]?steadyJump:switchJump).push(jump);
  }
  const mean=a=>a.length?a.reduce((sum,value)=>sum+value,0)/a.length:0;
  const out={label,frames:N,
    headHeight_px:{ref:H0,mean:+hs.mean.toFixed(2),min:+hs.min.toFixed(2),max:+hs.max.toFixed(2)},
    vertical_squash_pct:{pp:+(100*(hs.max-hs.min)/H0).toFixed(3),
                         worst:+(100*(hs.min-H0)/H0).toFixed(3),
                         sd:+(100*hs.sd/H0).toFixed(3)},
    travel_sd_px:{crown:+stats(rec.crown).sd.toFixed(2),eyes:+stats(rec.eyes).sd.toFixed(2),
                  chin:+stats(rec.chin).sd.toFixed(2),collar:+stats(rec.collar).sd.toFixed(2)},
    travel_gain_vs_eyes:{crown:+(stats(rec.crown).sd/stats(rec.eyes).sd).toFixed(3),
                         chin:+(stats(rec.chin).sd/stats(rec.eyes).sd).toFixed(3),
                         collar:+(stats(rec.collar).sd/stats(rec.eyes).sd).toFixed(3)},
    cheek:{states_used:[...new Set(rec.ck)].sort((a,b)=>a-b),
           changes_per_sec:+chg(rec.ck).toFixed(2),
           value:{min:+stats(rec.ckv).min.toFixed(3),max:+stats(rec.ckv).max.toFixed(3),
                  sd:+stats(rec.ckv).sd.toFixed(3)}},
    brow:{states_used:[...new Set(rec.bw)].sort((a,b)=>a-b),
          changes_per_sec:+chg(rec.bw).toFixed(2)},
    viseme_head_coupling:{switch_mean_px:+mean(switchJump).toFixed(4),
                          steady_mean_px:+mean(steadyJump).toFixed(4),
                          ratio:+(mean(switchJump)/Math.max(mean(steadyJump),1e-6)).toFixed(2),
                          direct_drive_max_px:+Math.max(...rec.headDrive).toFixed(4)},
    mouth_timing:{changes_per_sec:+chg(rec.viseme).toFixed(2),
                  min_pose_ms:+(P.minPose*1000).toFixed(1),
                  texture_crossfade_ms:+(P.xfade*1000).toFixed(1)},
    render_seams:{min_band_overlap_px:+Math.min(...rec.bandOverlap).toFixed(2)},
    eyelid:{active_pct:+(100*rec.lid.reduce((sum,value)=>sum+value,0)/N).toFixed(2),
             longest_run_ms:+(longestRun(rec.lid)*1000/60).toFixed(1)},
    blink:{count:blinkStarts.length,
           min_gap_ms:blinkGaps.length?+Math.min(...blinkGaps).toFixed(1):null}};
  return out;
}

(async()=>{
  for(let i=0;i<40;i++)await new Promise(r=>setTimeout(r,5));
  if(!P.ready){console.error('NOT READY');process.exit(2);}
  NOW=1000;
  const a=await run('listening',45,false);
  const b=await run('speaking',45,true);
  for(const result of [a,b]){
    if(result.render_seams.min_band_overlap_px<1.5)
      throw new Error(`${result.label}: raster seam overlap ${result.render_seams.min_band_overlap_px}px`);
    if(result.blink.min_gap_ms!==null&&result.blink.min_gap_ms<1000)
      throw new Error(`${result.label}: rapid double blink (${result.blink.min_gap_ms}ms)`);
    if(result.eyelid.longest_run_ms>650)
      throw new Error(`${result.label}: eyelid remains active for ${result.eyelid.longest_run_ms}ms`);
  }
  if(b.eyelid.active_pct>35)
    throw new Error(`speaking: eyelid active ${b.eyelid.active_pct}% of frames`);
  if(b.viseme_head_coupling.direct_drive_max_px>0)
    throw new Error(`speaking: visemes directly drive head Y by ${b.viseme_head_coupling.direct_drive_max_px}px`);
  if(b.viseme_head_coupling.ratio>1.15)
    throw new Error(`speaking: head jumps ${b.viseme_head_coupling.ratio}x harder on viseme switches`);
  if(b.mouth_timing.texture_crossfade_ms>20)
    throw new Error(`speaking: mouth texture crossfade lasts ${b.mouth_timing.texture_crossfade_ms}ms`);
  if(b.mouth_timing.changes_per_sec>10.5)
    throw new Error(`speaking: mouth changes ${b.mouth_timing.changes_per_sec} times per second`);
  console.log(JSON.stringify({before_or_after:process.argv[3]||'run',runs:[a,b]},null,1));
  process.exit(0);
})();
