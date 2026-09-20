
"use strict";
document.addEventListener("DOMContentLoaded", () => {
    if (typeof QWebChannel !== "undefined") {
        new QWebChannel(qt.webChannelTransport, function(channel) {
            window.bridge = channel.objects.bridge;
            window.bridge.stateChanged.connect(window.updateState);
            window.bridge.eventPushed.connect(function(eventStr) {
                const e = JSON.parse(eventStr);
                toast(e.sev, e.src, e.msg);
            });
            window.bridge.ready();
        });
    }
});
window.updateState = function(stateStr) {
    const s = typeof stateStr === 'string' ? JSON.parse(stateStr) : stateStr;
    if (s.dl !== undefined) {
      state.dlH.push(s.dl); if (state.dlH.length > HIST) state.dlH.shift();
      state.ulH.push(s.ul); if (state.ulH.length > HIST) state.ulH.shift();
    }
    if (s.dht && s.dht.nodes !== undefined) {
      state.dhtH.push(s.dht.nodes); if (state.dhtH.length > HIST) state.dhtH.shift();
    }
    const oldTorrents = state.torrents.map(t=>t.id).join(",");
    if (s.torrents) {
      s.torrents.forEach(t => {
         const existing = state.torrents.find(x => x.id === t.id);
         t.dlH = existing ? existing.dlH : [];
         t.ulH = existing ? existing.ulH : [];
         t.dlH.push(t.dl || 0); if (t.dlH.length > HIST) t.dlH.shift();
         t.ulH.push(t.ul || 0); if (t.ulH.length > HIST) t.ulH.shift();
      });
    }
    Object.assign(state, s);
    state.torrents.forEach(t => {
      const r = rowRefs.get(t.id);
      if (r) t._open = r.el.classList.contains("open");
    });
    const newTorrents = state.torrents.map(t=>t.id).join(",");
    if (oldTorrents !== newTorrents) {
        if (state.view === "lib") structLib();
        if (state.view === "dash") renderDash();
    }
    renderDynamic();
};

/* roundRect polyfill for older WebEngine/Chromium */
if(!CanvasRenderingContext2D.prototype.roundRect){
  CanvasRenderingContext2D.prototype.roundRect=function(x,y,w,h,r){r=Math.min(r,w/2,h/2);
    this.moveTo(x+r,y);this.arcTo(x+w,y,x+w,y+h,r);this.arcTo(x+w,y+h,x,y+h,r);
    this.arcTo(x,y+h,x,y,r);this.arcTo(x,y,x+w,y,r);this.closePath();return this};}
/* ═══ helpers ═══════════════════════════════════════════════════════════ */
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const R=(a,b)=>a+Math.random()*(b-a), RI=(a,b)=>Math.floor(R(a,b+1)), pick=a=>a[Math.floor(Math.random()*a.length)];
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const MONO='"JetBrains Mono","Cascadia Code",Consolas,monospace';
function fmtBytes(v){if(!isFinite(v))return"—";const u=["B","KiB","MiB","GiB","TiB"];let i=0;while(v>=1024&&i<4){v/=1024;i++}return(i?v.toFixed(2):Math.round(v))+" "+u[i]}
function fmtRate(v){if(v<1)return Math.round(v)+" B/s";const u=["B/s","KiB/s","MiB/s","GiB/s"];let i=0;while(v>=1024&&i<3){v/=1024;i++}return v.toFixed(2)+" "+u[i]}
function fmtRateSplit(v){if(v<1)return[Math.round(v),"B/s"];const u=["B/s","KiB/s","MiB/s","GiB/s"];let i=0;while(v>=1024&&i<3){v/=1024;i++}return[v.toFixed(2),u[i]]}
function fmtEta(s){if(!isFinite(s)||s<=0)return"—";if(s<60)return Math.round(s)+"s";if(s<3600)return Math.floor(s/60)+"m "+Math.round(s%60)+"s";return Math.floor(s/3600)+"h "+Math.round(s%3600/60)+"m"}
function fmtUp(s){s=Math.floor(s);if(s<60)return s+"s";if(s<3600)return Math.floor(s/60)+"m "+(s%60)+"s";return Math.floor(s/3600)+"h "+Math.floor(s%3600/60)+"m"}
const clock=()=>new Date().toLocaleTimeString("en-GB",{hour12:false});
const MiB=1048576, KiB=1024, PSIZE=256*KiB;

/* ═══ state ═════════════════════════════════════════════════════════════ */
const HIST=120;
const state={view:"dash",tab:"ov",selected:null,filter:"",logSev:"all",logQ:"",
  uptime:0,sDown:0,sUp:0,dlH:[],ulH:[],dhtH:[],events:[],counters:{info:0,ok:0,warn:0,err:0},
  dht:{nodes:0,found:0,buckets:new Array(160).fill(0)},torrents:[],idSeq:1};
const CLIENTS=["qBittorrent 4.6.6","libtorrent 2.0.9","Transmission 4.0.5","PeerStream 0.1","Deluge 2.1.1","aria2 1.37"];
const POOL=[["Sintel 4K Open Movie",1.32*1024*MiB,[["sintel-2160p.mp4",1.05*1024*MiB],["sintel-1080p.mp4",412*MiB],["sintel.en.srt",64*KiB],["README.txt",4*KiB]]],
 ["Tears of Steel 4K",918*MiB,[["tears-of-steel-2160p.mkv",880*MiB],["credits.pdf",2.1*MiB]]],
 ["Cosmos Laundromat 4K",642*MiB,[["cosmos-laundromat-2160p.mp4",640*MiB],["poster.png",2.4*MiB]]],
 ["Big.Buck.Bunny.Collection",1.9*1024*MiB,[["bbb-2160p.mp4",1.1*1024*MiB],["bbb-1080p.mp4",640*MiB],["extras/behind-the-scenes.mp4",180*MiB]]]];
function mkPeers(n){const p=[];for(let i=0;i<n;i++)p.push({ip:`${RI(11,223)}.${RI(0,255)}.${RI(0,255)}.${RI(1,254)}:${RI(1024,65000)}`,client:pick(CLIENTS),dl:R(20,420)*KiB,ul:R(0,180)*KiB,prog:R(.05,1),held:RI(1,9)});return p}
function mkTorrent(name,size,files,prog,stateName){
  const pieces=Math.max(1,Math.round(size/PSIZE)), have=Math.floor(pieces*prog);
  const ps=new Uint8Array(pieces); for(let i=0;i<have;i++)ps[i]=3;
  for(let i=have;i<Math.min(have+6,pieces);i++)ps[i]=i<have+2?2:1;
  const t={id:state.idSeq++,name,size,pieces,have,ps,frac:0,state:stateName,
    dl:stateName==="downloading"?R(.8,1.8)*MiB:0, ul:stateName==="seeding"?R(.3,.9)*MiB:0,
    base:R(.9,1.7)*MiB, dlH:[],ulH:[],peers:mkPeers(stateName==="paused"?0:RI(4,9)),
    downloaded:have*PSIZE,uploaded:R(0,.4)*have*PSIZE,added:Date.now()-RI(200,9000)*1000,
    savePath:"C:\\Users\\ssake\\Downloads",
    files:files.map(f=>({name:f[0],size:f[1],prio:"normal"})),
    trackers:[{url:"udp://tracker.openbittorrent.com:6969/announce",st:"ok",peers:RI(20,240),next:RI(10,900)},
              {url:"https://tracker.peerstream.dev:443/announce",st:"ok",peers:RI(5,90),next:RI(10,900)},
              {url:"udp://dht.peerstream.dev:6881 (DHT fallback)",st:"idle",peers:0,next:0}]};
  for(let i=0;i<HIST;i++){t.dlH.push(t.state==="downloading"?t.base*R(.6,1.3):0);t.ulH.push(t.state==="seeding"?t.ul*R(.6,1.3):0)}
  return t;
}
// state.torrents.push(mkTorrent("Big Buck Bunny",263.64*MiB,[["big-buck-bunny-1080p.mp4",263.6*MiB],["bbb.en.srt",58*KiB]],.043,"downloading"));
// state.torrents.push(mkTorrent("Ubuntu 24.04.3 Desktop amd64",5.6*1024*MiB,[["ubuntu-24.04.3-desktop-amd64.iso",5.6*1024*MiB]],1,"seeding"));
// state.torrents.push(mkTorrent("Arch Linux 2026.09.01 x86_64",1.12*1024*MiB,[["archlinux-2026.09.01-x86_64.iso",1.12*1024*MiB]],.62,"downloading"));
// state.torrents.push(mkTorrent("Sintel 1080p Open Movie",412*MiB,[["sintel-1080p.mp4",412*MiB]],.31,"paused"));
// mock history
// mock dht buckets
state.dht.nodes=state.dht.buckets.reduce((a,b)=>a+b,0); state.dht.found=RI(8,26);

/* ═══ events ═══════════════════════════════════════════════════════════ */
function pushEvent(sev,src,msg,tid){
  const ev={t:clock(),sev,src,msg,tid:tid||null};
  state.events.push(ev); if(state.events.length>400)state.events.shift();
  state.counters[sev==="ok"?"ok":sev]++;
  appendLog(ev);
}
function rndIp(){return`${RI(11,223)}.${RI(0,255)}.${RI(0,255)}.${RI(1,254)}:${RI(1024,65000)}`}
function simEvents(){
  const ts=state.torrents.filter(t=>t.state!=="paused");
  if(Math.random()<.5&&ts.length){const t=pick(ts);
    pushEvent(pick(["info","info","info","ok"]),pick(["PEER","DISK","ENGINE"]),pick([
      `Connected to ${rndIp()} (${pick(CLIENTS)})`,
      `Wrote block piece ${RI(0,t.pieces)}:${RI(0,15)} · ${fmtBytes(16*KiB)}`,
      `Rarest-first scheduled piece ${RI(0,t.pieces)} (availability ${RI(1,6)})`,
      `Announce OK — ${RI(8,220)} peers returned`,
      `DHT ping ${rndIp()} rtt ${RI(18,180)} ms`,
      `Peer bitfield received · ${RI(2,40)} pieces held`]),t.id)}
  if(Math.random()<.035&&ts.length)pushEvent("warn","PEER",`Choke received from ${rndIp()} — re-requesting block`,pick(ts).id);
  if(Math.random()<.02)pushEvent("warn","TRACKER",`Announce slow (${RI(2200,6800)} ms) — udp://tracker.openbittorrent.com:6969`,null);
  if(Math.random()<.008)  {pushEvent("err","TRACKER",`Tracker failed: https://tracker.example:443/announce — connect timed out after 10.0s`,null);
    if(toastsOn())toast("err","Tracker failed","tracker.example:443 timed out after 10 s — falling back to DHT.");}
}

/* ═══ toasts ════════════════════════════════════════════════════════════ */
const toastsOn=()=>{const c=$("#setToasts");return !c||c.checked};
function toast(sev,title,msg){
  if(!toastsOn())return;
  const ic={ok:"i-check",warn:"i-warn",err:"i-err",info:"i-bolt"}[sev];
  const el=document.createElement("div");el.className="toast "+sev;
  el.innerHTML=`<span class="tib"><svg class="ic sm"><use href="#${ic}"/></svg></span>
    <div style="min-width:0"><div class="tt">${title}</div><div class="tm">${msg}</div></div>
    <button class="tx"><svg class="ic sm"><use href="#i-x"/></svg></button><span class="tb"></span>`;
  el.querySelector(".tx").onclick=()=>kill();
  $("#toasts").appendChild(el);
  const to=setTimeout(kill,4600);
  function kill(){clearTimeout(to);el.classList.add("out");setTimeout(()=>el.remove(),240)}
  while($("#toasts").children.length>4)$("#toasts").firstChild.remove();
}

/* ═══ charts ════════════════════════════════════════════════════════════ */
function niceMax(v){if(v<=0)return 1;const p=Math.pow(10,Math.floor(Math.log10(v)));const n=v/p;return(n<=1?1:n<=2?2:n<=5?5:10)*p}
function drawChart(cv,series,fmt){
  const w=cv.clientWidth,h=cv.clientHeight;if(!w||!h)return;
  const dpr=window.devicePixelRatio||1;
  if(cv.width!==Math.round(w*dpr)||cv.height!==Math.round(h*dpr)){cv.width=Math.round(w*dpr);cv.height=Math.round(h*dpr)}
  const x=cv.getContext("2d");x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,w,h);
  let m=0;series.forEach(s=>s.data.forEach(v=>{if(v>m)m=v}));m=niceMax(m*1.15)||1;
  x.strokeStyle=THEME.grid;x.lineWidth=1;x.setLineDash([3,5]);
  for(let i=0;i<=4;i++){const y=8+(h-24)*i/4;x.beginPath();x.moveTo(0,y);x.lineTo(w,y);x.stroke()}
  x.setLineDash([]);x.font="9.5px "+MONO;x.fillStyle=THEME.label;
  x.fillText(fmt(m),4,12);x.fillText("0",4,h-6);x.textAlign="right";x.fillText("now",w-4,h-6);x.textAlign="left";
  series.forEach(s=>{
    const n=s.data.length;if(n<2)return;
    const px=i=>i*(w/(n-1)), py=v=>8+(h-24)*(1-v/m);
    if(s.fill){const g=x.createLinearGradient(0,0,0,h);g.addColorStop(0,s.fill);g.addColorStop(1,"rgba(0,0,0,0)");
      x.beginPath();x.moveTo(0,h-16);series[0].data.forEach((_,i)=>x.lineTo(px(i),py(s.data[i])));x.lineTo(w,h-16);x.closePath();x.fillStyle=g;x.fill()}
    x.beginPath();s.data.forEach((v,i)=>i?x.lineTo(px(i),py(v)):x.moveTo(px(i),py(v)));
    x.strokeStyle=s.color;x.lineWidth=1.7;x.lineJoin="round";x.stroke();
    const lv=s.data[n-1];x.beginPath();x.arc(px(n-1)-1,py(lv),2.6,0,7);x.fillStyle=s.color;x.shadowColor=s.color;x.shadowBlur=9;x.fill();x.shadowBlur=0;
  });
}
function drawSpark(cv,data,color){
  const w=cv.clientWidth||56,h=cv.clientHeight||24,dpr=window.devicePixelRatio||1;
  if(cv.width!==Math.round(w*dpr)){cv.width=Math.round(w*dpr);cv.height=Math.round(h*dpr)}
  const x=cv.getContext("2d");x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,w,h);
  let m=0;data.forEach(v=>{if(v>m)m=v});if(m<=0)m=1;
  x.beginPath();data.forEach((v,i)=>{const px=i*(w/(data.length-1)),py=h-2-(h-5)*(v/m);i?x.lineTo(px,py):x.moveTo(px,py)});
  x.strokeStyle=color;x.lineWidth=1.4;x.lineJoin="round";x.stroke();
  x.lineTo(w,h);x.lineTo(0,h);x.closePath();const g=x.createLinearGradient(0,0,0,h);g.addColorStop(0,color.replace(")",",.25)").replace("rgb","rgba"));g.addColorStop(1,"rgba(0,0,0,0)");x.fillStyle=g;x.fill();
}
const THEMES={
 dark:  {em:"rgb(52,211,153)",am:"rgb(251,191,36)",emF:"rgba(16,185,129,.20)",amF:"rgba(245,158,11,.12)",grid:"rgba(255,255,255,.055)",label:"#66726D",pieces:["#202624","#FBBF24","#6EE7B7","#10B981"],cell:"#1A201E",bucket:"16,185,129"},
 light: {em:"rgb(5,150,105)", am:"rgb(180,83,9)",  emF:"rgba(5,150,105,.15)", amF:"rgba(217,119,6,.13)", grid:"rgba(9,30,24,.10)",  label:"#7C8B85",pieces:["#E1E8E4","#D97706","#34D399","#059669"],cell:"#E7ECE9",bucket:"5,150,105"},
 amoled:{em:"rgb(60,224,168)",am:"rgb(255,197,61)",emF:"rgba(60,224,168,.22)",amF:"rgba(255,197,61,.14)",grid:"rgba(255,255,255,.075)",label:"#767F7B",pieces:["#161616","#FFC53D","#6EE7B7","#10B981"],cell:"#141414",bucket:"60,224,168"}};
let THEME_NAME="dark", THEME=THEMES.dark, EM=THEME.em, AM=THEME.am, EMF=THEME.emF, AMF=THEME.amF;

/* ═══ simulation tick (5 Hz, mirrors StatePump) ═════════════════════════ */
function tick(){
  state.uptime+=.2; let gd=0,gu=0;
  state.torrents.forEach(t=>{
    if(t.state==="downloading"){
      t.dl=clamp(t.dl+ (Math.random()-.5)*.22*t.base, .18*t.base, 1.7*t.base);
      t.ul=clamp(t.ul+(Math.random()-.5)*30*KiB, 0, 300*KiB);
      const bytes=t.dl*.2; t.downloaded+=bytes; t.uploaded+=t.ul*.2; t.frac+=bytes/PSIZE;
      while(t.frac>=1&&t.have<t.pieces){t.frac-=1;
        let idx=-1;for(let i=0;i<t.pieces;i++){if(t.ps[i]===2){idx=i;break}}
        if(idx<0)for(let i=0;i<t.pieces;i++){if(t.ps[i]===1){idx=i;break}}
        if(idx<0)idx=t.have;
        t.ps[idx]=3;t.have++;
        for(let k=0;k<2;k++){const m=RI(0,t.pieces-1);if(t.ps[m]===0)t.ps[m]=Math.random()<.5?1:2}}
      if(t.have>=t.pieces){t.state="seeding";t.dl=0;
        pushEvent("ok","ENGINE",`Completed "${t.name}" — all ${t.pieces} pieces verified`,t.id);
        toast("ok","Download complete",`${t.name} · ${fmtBytes(t.size)} verified on disk.`);
        structLib();}
      if(Math.random()<.02&&t.peers.length<12)t.peers.push(...mkPeers(1));
    }else if(t.state==="seeding"){
      t.ul=clamp(t.ul+(Math.random()-.5)*.2*Math.max(t.ul,400*KiB), 60*KiB, 3*MiB);
      t.dl=0; t.uploaded+=t.ul*.2;
    }else{t.dl=0;t.ul=0}
    if(t.state!=="paused"){t.peers.forEach(p=>{p.dl=clamp(p.dl+(Math.random()-.5)*40*KiB,0,900*KiB);p.ul=clamp(p.ul+(Math.random()-.5)*22*KiB,0,400*KiB)});
      if(Math.random()<.01&&t.peers.length)  t.peers.splice(RI(0,t.peers.length-1),1);
      if(Math.random()<.012&&t.peers.length<12)t.peers.push(...mkPeers(1));}
    gd+=t.dl;gu+=t.ul;
    t.dlH.push(t.dl);t.ulH.push(t.ul);if(t.dlH.length>HIST)t.dlH.shift(),t.ulH.shift();
    t.trackers.forEach(tr=>{if(tr.next>0){tr.next-=.2;if(tr.next<=0){tr.next=RI(300,1500);tr.peers=RI(5,240)}}});
  });
  state.sUp+=gu*.2; state.sDown+=gd*.2;
  state.dlH.push(gd);state.ulH.push(gu);if(state.dlH.length>HIST){state.dlH.shift();state.ulH.shift()}
  if(Math.random()<.3){state.dht.nodes=clamp(state.dht.nodes+RI(-2,4),40,900);
    const b=RI(0,159);state.dht.buckets[b]=clamp(state.dht.buckets[b]+(Math.random()<.6?1:-1),0,8);
    if(Math.random()<.25)state.dht.found++;}
  state.dhtH.push(state.dht.nodes);if(state.dhtH.length>HIST)state.dhtH.shift();
  simEvents();
  renderDynamic();
}

/* ═══ render: top / dash ════════════════════════════════════════════════ */
function activeTs(){return state.torrents.filter(t=>t.state!=="paused")}
function renderTop(){
  const gd=state.dlH[state.dlH.length-1],gu=state.ulH[state.ulH.length-1];
  $("#chipDl").textContent=fmtRate(gd);$("#chipUl").textContent=fmtRate(gu);
  drawSpark($("#chipDlSpark"),state.dlH.slice(-40),EM);drawSpark($("#chipUlSpark"),state.ulH.slice(-40),AM);
  const peers=state.torrents.reduce((a,t)=>a+t.peers.length,0);
  const eta=Math.max(...state.torrents.filter(t=>t.state==="downloading").map(t=>(t.size-t.have*PSIZE)/Math.max(t.dl,1)),0);
  $("#sessionPill").innerHTML=state.torrents.length?
    `<b>${activeTs().length}</b> of ${state.torrents.length} active<span class="sep"></span><b>${peers}</b> peers<span class="sep"></span><b>${fmtBytes(state.sDown)}</b> down<span class="sep"></span>eta <b>${fmtEta(eta)}</b>`
    :`no torrents`;
  $("#badgeDash").textContent=activeTs().length;$("#badgeLib").textContent=state.torrents.length;
}
function renderDash(){
  const gd=state.dlH[state.dlH.length-1],gu=state.ulH[state.ulH.length-1];
  const[v,u]=fmtRateSplit(gd);$("#kDl").innerHTML=`${v}<span class="u">${u}</span>`;
  const[v2,u2]=fmtRateSplit(gu);$("#kUl").innerHTML=`${v2}<span class="u">${u2}</span>`;
  $("#kDlSub").innerHTML=`<b>${fmtBytes(state.sDown)}</b> this session`;
  $("#kUlSub").innerHTML=`<b>${fmtBytes(state.sUp)}</b> this session`;
  const peers=state.torrents.reduce((a,t)=>a+t.peers.length,0);
  $("#kPeers").textContent=peers;$("#kPeersSub").textContent=peers?`across ${activeTs().length} active torrents`:"none connected";
  const ratio=state.sDown>0?state.sUp/state.sDown:null;
  $("#kRatio").textContent=ratio===null?"——":ratio.toFixed(2);
  $("#kRatioSub").textContent=ratio===null?"no torrents yet":ratio>=1?"giving back more than taking":"leeching — keep seeding";
  drawChart($("#globalChart"),[{data:state.dlH,color:EM,fill:EMF},{data:state.ulH,color:AM,fill:AMF}],fmtRate);
  const serving=state.torrents.reduce((a,t)=>a+t.peers.filter(p=>p.held>3).length,0);
  const totPieces=state.torrents.reduce((a,t)=>a+t.pieces,0),verPieces=state.torrents.reduce((a,t)=>a+t.have,0);
  $("#hPeersV").textContent=serving?`${serving} peers`:"—";$("#hPeersB").style.width=clamp(serving/40*100,0,100)+"%";
  $("#hPiecesV").textContent=totPieces?`${(verPieces/totPieces*100).toFixed(1)}%`:"—";$("#hPiecesB").style.width=(totPieces?verPieces/totPieces*100:0)+"%";
  $("#hActV").textContent=`${activeTs().length} / ${state.torrents.length}`;$("#hActB").style.width=(state.torrents.length?activeTs().length/state.torrents.length*100:0)+"%";
  $("#dashCount").textContent=state.torrents.length?`${state.torrents.length} in library`:"";
  $("#dashList").innerHTML=state.torrents.slice(0,5).map(t=>{
    const pr=t.have/t.pieces*100;
    return`<div class="mini" data-open="${t.id}"><span style="color:${t.state==="paused"?"var(--fnt)":t.state==="seeding"?"var(--am)":"var(--em)"}"><svg class="ic sm"><use href="#${t.state==="paused"?"i-pause":t.state==="seeding"?"i-up":"i-down"}"/></svg></span>
      <span class="nm">${t.name}</span><span class="bar ${t.state==="seeding"?"am":t.state==="paused"?"pause":""}"><i style="width:${pr}%"></i></span>
      <span class="rt"><span class="d">↓${fmtRate(t.dl)}</span> <span class="u">↑${fmtRate(t.ul)}</span></span></div>`}).join("")||
      `<div class="fnt" style="font-size:12px;padding:8px">No torrents in the library.</div>`;
}

/* ═══ render: library ═══════════════════════════════════════════════════ */
const rowRefs=new Map();
function structLib(){
  const wrap=$("#libRows");const q=state.filter.toLowerCase();
  const list=state.torrents.filter(t=>t.name.toLowerCase().includes(q));
  $("#libSub").textContent=`${state.torrents.length} torrent${state.torrents.length===1?"":"s"}${q?` · ${list.length} matching`:""}`;
  $("#libEmpty").style.display=state.torrents.length?"none":"flex";
  wrap.innerHTML="";rowRefs.clear();
  list.forEach(t=>{
    const el=document.createElement("div");el.className="t-row"+(state.selected===t.id?" sel":"")+(t._open?" open":"");el.dataset.id=t.id;
    el.innerHTML=`<div class="tgrid t-main">
      <svg class="ic sm chev"><use href="#i-chev"/></svg>
      <div style="min-width:0"><div class="t-name">${t.name}</div><div class="t-meta"><span>${fmtBytes(t.size)}</span><span class="jp">${t.have}/${t.pieces} pieces</span><span class="jpe">${t.peers.length} peers</span></div></div>
      <div><div class="prog ${t.state==="seeding"?"seed":t.state==="paused"?"pause":""}"><i class="pb" style="width:${t.have/t.pieces*100}%"></i></div><div class="t-pct pc">${(t.have/t.pieces*100).toFixed(1)}%</div></div>
      <div class="t-rate"><span class="d">↓ <span class="rd">${fmtRate(t.dl)}</span></span><br><span class="u">↑ <span class="ru">${fmtRate(t.ul)}</span></span></div>
      <div class="t-eta et">${fmtEta((t.size-t.have*PSIZE)/Math.max(t.dl,1))}</div>
      <div class="t-peers pe">${t.peers.length}</div>
      <div><span class="pill ${t.state==="downloading"?"down":t.state==="seeding"?"seed":"pause"} pi"><span class="pd"></span><span class="pt">${t.state}</span></span></div>
      <div class="t-actions">
        <button class="icon-btn ok pp" title="${t.state==="paused"?"Resume":"Pause"}"><svg class="ic sm"><use href="#${t.state==="paused"?"i-play":"i-pause"}"/></svg></button>
        <button class="icon-btn fo" title="Open file location"><svg class="ic sm"><use href="#i-folder"/></svg></button>
        <button class="icon-btn danger rm" title="Remove"><svg class="ic sm"><use href="#i-trash"/></svg></button>
      </div></div>
      <div class="t-x"><div>
          <div class="up" style="margin-bottom:8px">download · last 24 s</div><canvas class="spark-lg cx-d"></canvas>
          <div class="up" style="margin:10px 0 8px">upload · last 24 s</div><canvas class="spark-lg cx-u"></canvas></div>
        <div class="facts">
          <div class="fact"><div class="k">info hash</div><div class="v">${hashFor(t.id)}</div></div>
          <div class="fact"><div class="k">save path</div><div class="v">${t.savePath}</div></div>
          <div class="fact"><div class="k">piece size</div><div class="v">256.00 KiB</div></div>
          <div class="fact"><div class="k">added</div><div class="v">${new Date(t.added).toLocaleString("en-GB",{hour12:false})}</div></div>
          <div class="fact"><div class="k">downloaded</div><div class="v dd">${fmtBytes(t.downloaded)}</div></div>
          <div class="fact"><div class="k">uploaded</div><div class="v du">${fmtBytes(t.uploaded)}</div></div>
        </div>
        <div style="align-self:start"><button class="btn btn-ghost od" style="width:100%;justify-content:center">Open detail →</button></div>
      </div>`;
    el.querySelector(".t-main").addEventListener("click",e=>{
      if(e.target.closest(".icon-btn"))return;
      if(e.target.closest(".t-name")){selectTorrent(t.id);setView("detail");return}
      t._open=!t._open;el.classList.toggle("open",t._open);});
    el.querySelector(".pp").onclick=e=>{e.stopPropagation();togglePause(t.id)};
    el.querySelector(".fo").onclick=e=>{e.stopPropagation();toast("info","Opening Explorer",t.savePath+"\\"+t.files[0].name)};
    el.querySelector(".rm").onclick=e=>{e.stopPropagation();removeTorrent(t.id)};
    el.querySelector(".od").onclick=e=>{e.stopPropagation();selectTorrent(t.id);setView("detail")};
    wrap.appendChild(el);
    rowRefs.set(t.id,{el,pb:el.querySelector(".pb"),pc:el.querySelector(".pc"),rd:el.querySelector(".rd"),ru:el.querySelector(".ru"),
      et:el.querySelector(".et"),pe:el.querySelector(".pe"),pi:el.querySelector(".pi"),pt:el.querySelector(".pt"),
      jp:el.querySelector(".jp"),jpe:el.querySelector(".jpe"),dd:el.querySelector(".dd"),du:el.querySelector(".du"),
      cd:el.querySelector(".cx-d"),cu:el.querySelector(".cx-u")});
  });
}
function hashFor(id){let s="";const hex="0123456789abcdef";for(let i=0;i<40;i++)s+=hex[(id*7+i*13)%16];return s}
function updateLib(){
  state.torrents.forEach(t=>{const r=rowRefs.get(t.id);if(!r)return;
    const pr=t.have/t.pieces*100;
    r.pb.style.width=pr+"%";r.pc.textContent=pr.toFixed(1)+"%";
    r.rd.textContent=fmtRate(t.dl);r.ru.textContent=fmtRate(t.ul);
    r.et.textContent=t.state==="seeding"?"—":fmtEta((t.size-t.have*PSIZE)/Math.max(t.dl,1));
    r.pe.textContent=t.peers.length;r.jp.textContent=`${t.have}/${t.pieces} pieces`;r.jpe.textContent=`${t.peers.length} peers`;
    r.dd.textContent=fmtBytes(t.downloaded);r.du.textContent=fmtBytes(t.uploaded);
    const cls=t.state==="downloading"?"down":t.state==="seeding"?"seed":"pause";
    if(!r.pi.classList.contains(cls)){r.pi.className="pill "+cls+" pi";r.pt.textContent=t.state;
      r.el.querySelector(".prog").className="prog "+(t.state==="seeding"?"seed":t.state==="paused"?"pause":"");
      r.el.querySelector(".pp").innerHTML=`<svg class="ic sm"><use href="#${t.state==="paused"?"i-play":"i-pause"}"/></svg>`;}
    if(t._open&&r.cd.clientWidth){drawChart(r.cd,[{data:t.dlH,color:EM,fill:EMF}],fmtRate);
      drawChart(r.cu,[{data:t.ulH,color:AM,fill:AMF}],fmtRate);}
  });
}

/* ═══ render: detail ════════════════════════════════════════════════════ */
function selectTorrent(id){ state.selected=id; if(window.bridge) window.bridge.select_torrent(id); structLib(); }
function renderDetailStruct(){
  const t=state.torrents.find(x=>x.id===state.selected);
  $("#detEmpty").style.display=t?"none":"flex";$("#detWrap").style.display=t?"block":"none";
  if(!t)return;
  $("#detTitle").textContent=t.name;
  $("#detPause").querySelector("span").textContent=t.state==="paused"?"Resume":"Pause";
  $("#detPause").querySelector("use").setAttribute("href",t.state==="paused"?"#i-play":"#i-pause");
  $("#kvGrid").innerHTML=[
    ["info hash",`<span>${hashFor(t.id)}</span><button class="icon-btn cp" title="Copy"><svg class="ic sm"><use href="#i-copy"/></svg></button>`],
    ["save path",t.savePath],["piece size","256.00 KiB"],["pieces",`${t.pieces} total · ${t.have} verified`],
    ["added",new Date(t.added).toLocaleString("en-GB",{hour12:false})],["trackers",`${t.trackers.length} configured`],
    ["session download",fmtBytes(t.downloaded)],["session upload",fmtBytes(t.uploaded)]
  ].map(([k,v])=>`<div><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
  $("#kvGrid .cp")&&($("#kvGrid .cp").onclick=()=>toast("ok","Copied","info-hash on clipboard"));
  $("#fileRows").innerHTML=t.files.map(f=>{const fp=clamp(t.have/t.pieces+(Math.random()*.02),0,1);
    return`<tr><td style="color:var(--txt);font-weight:600"><svg class="ic sm" style="vertical-align:-3px;color:var(--fnt)"><use href="#i-file"/></svg> ${f.name}</td>
    <td class="b m">${fmtBytes(f.size)}</td><td><div class="prog ${t.state==="seeding"?"seed":""}"><i style="width:${(fp*100).toFixed(1)}%"></i></div></td>
    <td><select class="sel"><option ${f.prio==="normal"?"selected":""}>normal</option><option>high</option><option>skip</option></select></td></tr>`}).join("");
  $("#trkRows").innerHTML=t.trackers.map(tr=>`<div class="trk">
    <span style="color:${tr.st==="ok"?"var(--em)":"var(--fnt)"}"><svg class="ic sm"><use href="#i-globe"/></svg></span>
    <span class="url">${tr.url}</span>
    <span class="pill ${tr.st==="ok"?"down":"pause"}"><span class="pd"></span>${tr.st}</span>
    <span class="st">${tr.peers?tr.peers+" peers · ":""}${tr.next>0?"next in "+Math.round(tr.next)+"s":"idle"}</span></div>`).join("");
}
function renderDetailDyn(){
  const t=state.torrents.find(x=>x.id===state.selected);if(!t)return;
  const pr=t.have/t.pieces*100;
  $("#detSub").textContent=`${t.state} · ${pr.toFixed(1)}% · ${t.have}/${t.pieces} pieces · ${t.peers.length} peers`;
  $("#oProg").textContent=pr.toFixed(1)+"%";$("#oProgSub").textContent=`${fmtBytes(t.have*PSIZE)} of ${fmtBytes(t.size)}`;
  const[a,b]=fmtRateSplit(t.dl);$("#oDl").innerHTML=`${a}<span class="u"> ${b}</span>`;
  const[c,d]=fmtRateSplit(t.ul);$("#oUl").innerHTML=`${c}<span class="u"> ${d}</span>`;
  const eta=t.state==="downloading"?(t.size-t.have*PSIZE)/Math.max(t.dl,1):0;
  $("#oEta").textContent=t.state==="seeding"?"∞":fmtEta(eta);
  $("#oEtaSub").textContent=t.state==="seeding"?"complete — seeding":t.state==="paused"?"paused":"at current rate";
  if(state.tab==="ov")drawChart($("#ovChart"),[{data:t.dlH,color:EM,fill:EMF},{data:t.ulH,color:AM,fill:AMF}],fmtRate);
  if(state.tab==="peers"){
    $("#peerCount").textContent=`${t.peers.length} connected · ${t.peers.filter(p=>p.held>3).length} serving`;
    $("#peerRows").innerHTML=t.peers.map(p=>`<tr><td class="m" style="color:var(--txt)">${p.ip}</td><td>${p.client}</td>
      <td class="b m" style="color:var(--em)">↓ ${fmtRate(p.dl)}</td><td class="b m" style="color:var(--am)">↑ ${fmtRate(p.ul)}</td>
      <td><div class="prog"><i style="width:${(p.prog*100).toFixed(0)}%"></i></div></td><td class="b m">${p.held>3?"seeder":p.held+" pc"}</td></tr>`).join("")||
      `<tr><td colspan="6" class="fnt" style="padding:18px;text-align:center">no peers connected</td></tr>`;}
  if(state.tab==="pieces")drawPieces(t);
  if(state.tab==="trk")renderDetailStruct();
}
let pieceLayout=null;
function drawPieces(t){
  const cv=$("#pieceCanvas"),wrapW=cv.parentElement.clientWidth-36;if(wrapW<40)return;
  const cell=9,gap=3,step=cell+gap,cols=Math.floor(wrapW/step),rows=Math.ceil(t.pieces/cols);
  const dpr=window.devicePixelRatio||1,H=rows*step-gap+4;
  cv.style.height=H+"px";cv.width=Math.round(wrapW*dpr);cv.height=Math.round(H*dpr);
  const x=cv.getContext("2d");x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,wrapW,H);
  const COL=THEME.pieces;let cnt=[0,0,0,0];
  for(let i=0;i<t.pieces;i++){const s=t.ps[i];cnt[s]++;
    x.fillStyle=COL[s];x.globalAlpha=s===3?.92:1;
    x.beginPath();x.roundRect((i%cols)*step,Math.floor(i/cols)*step,cell,cell,2.5);x.fill()}
  x.globalAlpha=1;pieceLayout={cols,step,cell};
  $("#lgMiss").textContent=cnt[0];$("#lgReq").textContent=cnt[1];$("#lgDl").textContent=cnt[2];$("#lgVer").textContent=cnt[3];
  $("#pieceMeta").textContent=`${t.pieces} pieces · 256.00 KiB each · ${fmtBytes(t.size)} total · ${(t.have/t.pieces*100).toFixed(1)}% verified`;
}
$("#pieceCanvas").addEventListener("mousemove",e=>{
  const t=state.torrents.find(x=>x.id===state.selected);if(!t||!pieceLayout)return;
  const r=e.currentTarget.getBoundingClientRect();
  const cx=Math.floor((e.clientX-r.left)/pieceLayout.step),cy=Math.floor((e.clientY-r.top)/pieceLayout.step);
  const idx=cy*pieceLayout.cols+cx;
  if(idx<0||idx>=t.pieces)return;
  const s=["missing","requested","downloading","verified"][t.ps[idx]];
  $("#pieceInfo").innerHTML=`Piece <b style="color:var(--txt)">${idx}</b> · <span style="color:${t.ps[idx]===3?"var(--em)":t.ps[idx]===0?"var(--fnt)":"var(--am)"}">${s}</span> · 256.00 KiB · held by ${t.ps[idx]===3?0:RI(1,6)} peer(s) · offset ${fmtBytes(idx*PSIZE)}`;
});

/* ═══ render: log ═══════════════════════════════════════════════════════ */
function logRowHtml(ev){return`<div class="log-row ${ev.sev}"><span class="t">${ev.t}</span><span class="sv ${ev.sev}"></span><span class="src">${ev.src}</span><span class="msg">${ev.msg}</span></div>`}
const logVisible=ev=>(state.logSev==="all"||ev.sev===state.logSev)&&(!state.logQ||ev.msg.toLowerCase().includes(state.logQ));
function appendLog(ev){
  if(!logVisible(ev))return;
  const f=$("#logFeed");f.insertAdjacentHTML("beforeend",logRowHtml(ev));
  while(f.children.length>300)f.firstChild.remove();
  if($("#logAuto").checked)f.scrollTop=f.scrollHeight;
  const t=state.torrents.find(x=>x.id===ev.tid);
  if(ev.tid&&t&&ev.tid===state.selected){const d=$("#dlogFeed");d.insertAdjacentHTML("beforeend",logRowHtml(ev));while(d.children.length>200)d.firstChild.remove();d.scrollTop=d.scrollHeight}
  const tot=Math.max(state.counters.info+state.counters.ok,state.counters.warn,state.counters.err,1);
  $("#cInfo").textContent=state.counters.info+state.counters.ok;$("#cWarn").textContent=state.counters.warn;$("#cErr").textContent=state.counters.err;
  $("#cInfoB").style.width=((state.counters.info+state.counters.ok)/tot*100)+"%";
  $("#cWarnB").style.width=(state.counters.warn/tot*100)+"%";$("#cErrB").style.width=(state.counters.err/tot*100)+"%";
}
function renderLogFull(){
  $("#logFeed").innerHTML=state.events.filter(logVisible).map(logRowHtml).join("");
  $("#logFeed").scrollTop=$("#logFeed").scrollHeight;
  const t=state.torrents.find(x=>x.id===state.selected);
  $("#dlogFeed").innerHTML=state.events.filter(e=>e.tid===state.selected).map(logRowHtml).join("");
  $("#dlogFeed").scrollTop=$("#dlogFeed").scrollHeight;
}

/* ═══ render: dht ═══════════════════════════════════════════════════════ */
let stripBuilt=false;
function renderDht(){
  $("#hNodes").textContent=state.dht.nodes;$("#hNodesSub").textContent=`across 160 buckets · ${state.dht.nodes?Math.max(1,Math.round(state.dht.nodes/24))+" per bucket avg":"empty"}`;
  $("#hBuckets").textContent=state.dht.buckets.filter(b=>b>0).length;
  $("#hFound").textContent=state.dht.found;$("#hUp").textContent=fmtUp(state.uptime);
  drawChart($("#dhtChart"),[{data:state.dhtH,color:EM,fill:EMF}],v=>Math.round(v)+" nodes");
  const strip=$("#bucketStrip");
  if(!stripBuilt){strip.innerHTML=state.dht.buckets.map((b,i)=>`<i data-b="${i}" title="bucket ${i} · ${b}/8"></i>`).join("");stripBuilt=true}
  [...strip.children].forEach((el,i)=>{const b=state.dht.buckets[i];
    el.style.background=b===0?THEME.cell:b>=8?"var(--am2)":`rgba(${THEME.bucket},${.22+.68*(b/8)})`;
    el.title=`bucket ${i} · ${b}/8 nodes`});
  $("#nodeRows").innerHTML=Array.from({length:7},(_,i)=>{const b=RI(0,159);
    return`<tr><td class="m" style="color:var(--txt)">${hashFor(b+3).slice(0,16)}…</td><td class="m">${rndIp()}</td>
    <td class="b m">${RI(14,220)} ms</td><td class="b m">${RI(0,58)}s ago</td>
    <td><span class="pill ${Math.random()<.8?"down":"pause"}"><span class="pd"></span>${Math.random()<.8?"good":"questionable"}</span></td></tr>`}).join("");
}

/* ═══ actions ═══════════════════════════════════════════════════════════ */
function togglePause(id){ if(window.bridge) window.bridge.toggle_torrent(id); return; const t=state.torrents.find(x=>x.id===id);if(!t)return;
  if(t.state==="paused"){t.state=t.have>=t.pieces?"seeding":"downloading";pushEvent("ok","ENGINE",`Resumed "${t.name}"`,id);toast("info","Resumed",t.name)}
  else{t._prev=t.state;t.state="paused";t.dl=0;t.ul=0;pushEvent("warn","ENGINE",`Paused "${t.name}"`,id);toast("warn","Paused",t.name)}
  structLib();renderDetailStruct();}
function removeTorrent(id){ if(window.bridge) window.bridge.remove_torrent(id); return; const t=state.torrents.find(x=>x.id===id);if(!t)return;
  state.torrents=state.torrents.filter(x=>x.id!==id);
  if(state.selected===id)state.selected=null;
  pushEvent("warn","ENGINE",`Removed "${t.name}" from library (files kept on disk)`,null);
  toast("warn","Torrent removed",`${t.name} — downloaded files were kept.`);
  structLib();renderDetailStruct();}
function addTorrent(name,size,files){
  const t=mkTorrent(name,size,files,0,"downloading");t.peers=[];t.dl=0;
  state.torrents.unshift(t);
  pushEvent("ok","ENGINE",`Added "${name}" · ${t.pieces} pieces · metadata parsed`,t.id);
  toast("ok","Torrent added",`${name} · contacting ${t.trackers.length} trackers + DHT.`);
  structLib();setView("lib");}

/* ═══ view / tab wiring ═════════════════════════════════════════════════ */
function setView(v){
  state.view=v;
  $$(".nav-item").forEach(b=>b.classList.toggle("on",b.dataset.view===v));
  $$(".view").forEach(s=>s.classList.toggle("on",s.id==="v-"+v));
  if(v==="lib")structLib();
  if(v==="detail")renderDetailStruct();
  if(v==="log")renderLogFull();
  if(v==="dht")renderDht();
  if(v==="dash")renderDash();
  $("#content").scrollTop=0;
}
$$(".nav-item").forEach(b=>b.onclick=()=>setView(b.dataset.view));
$("#detTabs").addEventListener("click",e=>{const b=e.target.closest(".tab");if(!b)return;
  state.tab=b.dataset.tab;
  $$("#detTabs .tab").forEach(t=>t.classList.toggle("on",t===b));
  $$(".pane").forEach(p=>p.classList.toggle("on",p.dataset.pane===state.tab));
  renderDetailDyn();});
$("#libFilter").addEventListener("input",e=>{state.filter=e.target.value;structLib()});
$("#logChips").addEventListener("click",e=>{const b=e.target.closest(".fchip");if(!b)return;
  state.logSev=b.dataset.sev;$$("#logChips .fchip").forEach(c=>c.classList.toggle("on",c===b));renderLogFull()});
$("#logSearch").addEventListener("input",e=>{state.logQ=e.target.value.toLowerCase();renderLogFull()});
$("#dashList").addEventListener("click",e=>{const m=e.target.closest("[data-open]");if(m){selectTorrent(+m.dataset.open);setView("detail")}});
$("#detGoLib").onclick=()=>setView("lib");
$("#detPause").onclick=()=>state.selected&&togglePause(state.selected);
$("#detRemove").onclick=()=>state.selected&&removeTorrent(state.selected);
function setTheme(n){
  THEME_NAME=n; THEME=THEMES[n]; EM=THEME.em; AM=THEME.am; EMF=THEME.emF; AMF=THEME.amF;
  document.documentElement.setAttribute("data-theme",n);
  $("#themeIcon").setAttribute("href","#"+{dark:"i-moon",light:"i-sun",amoled:"i-amoled"}[n]);
  $("#themeLbl").textContent={dark:"Dark",light:"Light",amoled:"AMOLED"}[n];
  $$("#segTheme button").forEach(b=>b.classList.toggle("on",b.dataset.th===n));
  try{localStorage.setItem("ps-theme",n)}catch(e){}
  renderDynamic(); if(state.view==="dht")renderDht();
  if(state.view==="detail"&&state.tab==="pieces")renderDetailDyn();
}
$("#themeChip").onclick=()=>setTheme({dark:"light",light:"amoled",amoled:"dark"}[THEME_NAME]);
$("#segTheme").addEventListener("click",e=>{const b=e.target.closest("button");if(b)setTheme(b.dataset.th)});
$("#btnAdd").onclick=$("#dashAdd").onclick=$("#libAdd").onclick=()=>{$("#modal").classList.add("on")};
$("#setBrowse").onclick=()=>{ if (window.bridge) window.bridge.browse_download_dir(); };
const limV=v=>v<=0?"unlimited":fmtRate(v/100*25*MiB);
$("#setDlLim").oninput=e=>$("#setDlLimV").textContent=limV(+e.target.value);
$("#setUlLim").oninput=e=>$("#setUlLimV").textContent=limV(+e.target.value);
$("#setPeers").oninput=e=>$("#setPeersV").textContent=e.target.value;
$("#setDir").oninput=e=>$("#footPath").textContent=e.target.value;

/* modal */
let mzMode="file",picked=null;
$$("[data-mz]").forEach(b=>b.onclick=()=>{mzMode=b.dataset.mz;
  $$("[data-mz]").forEach(x=>x.classList.toggle("on",x===b));
  $("#mzFile").style.display=mzMode==="file"?"block":"none";
  $("#mzMag").style.display=mzMode==="mag"?"block":"none";});
const dz=$("#dropzone");
dz.onclick=()=>{ if(window.bridge) window.bridge.browse_torrent_file(); $("#modal").classList.remove("on"); };
dz.ondragover=e=>{e.preventDefault();dz.classList.add("over")};
dz.ondragleave=()=>dz.classList.remove("over");
dz.ondrop=e=>{e.preventDefault();dz.classList.add("over");picked=pick(POOL);$("#dzName").textContent=(e.dataTransfer.files[0]?.name)||picked[0].toLowerCase().replace(/\s+/g,".")+".torrent"};
$("#mzCancel").onclick=()=>$("#modal").classList.remove("on");
$("#modal").addEventListener("click",e=>{if(e.target.id==="modal")$("#modal").classList.remove("on")});
document.addEventListener("keydown",e=>{if(e.key==="Escape")$("#modal").classList.remove("on")});
$("#mzAdd").onclick=()=>{
  $("#modal").classList.remove("on");
  if(mzMode==="mag"){
    const v=$("#magnetInput").value.trim();
    if(window.bridge && v) window.bridge.add_torrent(v);
    $("#magnetInput").value="";
    return;
  }
};

/* ═══ boot ══════════════════════════════════════════════════════════════ */
function renderDynamic(){
  renderTop();
  if(state.view==="dash")renderDash();
  if(state.view==="lib")updateLib();
  if(state.view==="detail")renderDetailDyn();
  if(state.view==="dht")renderDht();
}
structLib();renderTop();renderDash();
let _saved=null;try{_saved=localStorage.getItem("ps-theme")}catch(e){}
setTheme(THEMES[_saved]?_saved:"dark");
pushEvent("ok","ENGINE","Engine online — StatePump linked at 5 Hz, EventFeed streaming",null);
pushEvent("info","DHT","Bootstrapped via router.bittorrent.com · 8 nodes seeded",null);
setTimeout(()=>toast("ok","Engine online","StatePump @ 5 Hz · EventFeed batched · DHT bootstrapped."),700);
// setInterval(tick,200);
window.addEventListener("resize",()=>renderDynamic());
