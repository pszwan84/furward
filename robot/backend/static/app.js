const $=id=>document.getElementById(id);
const pretty=data=>JSON.stringify(data,null,2);
async function api(path,body){const response=await fetch('/api/'+path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const result=await response.json();if(!result.ok)throw new Error(result.error+'：'+result.message);return result.data;}
async function act(path,body={}){try{const data=await api(path,body);$('result').textContent='请求成功（以状态回执为准）';await refresh();return data;}catch(error){$('result').textContent=error.message;}}
async function refresh(){try{const [health,state,current]=await Promise.all([api('health'),api('robot/state'),api('nav/current')]);const identity=health.backend!=='bridge'?'Mock 模拟 · 未连接真机':!health.online?'Bridge 等待连接':state.simulated?'Bridge 模拟联调 · 未连接真机':health.physical_execution_enabled?'T1 真机执行已启用 · 尚未验收':'T1 真机桥接 · 运动未放行';$('connection').textContent='后端在线 · '+identity;$('state').textContent=pretty(state);$('route').textContent=pretty(current);const pose=current.nav?.estimated_pose;$('position').textContent='指令积分估算 · '+(pose?pretty(pose):'尚未初始化')+' · 非硬件实测';}catch(error){$('connection').textContent='后端不可用';$('result').textContent=error.message;}}
async function drawMap(){try{
  const map=await api('indoor/map'),svg=$('map');svg.replaceChildren();
  const ns='http://www.w3.org/2000/svg';
  function element(tag,attrs,text){const node=document.createElementNS(ns,tag);for(const [key,value] of Object.entries(attrs))node.setAttribute(key,value);if(text)node.textContent=text;svg.append(node);return node;}
  element('text',{x:18,y:24},'室内地图 · 米制示意（非硬件实测）');
  const points=map.nodes||[],maxX=Math.max(1,...points.map(p=>p.x)),maxY=Math.max(1,...points.map(p=>p.y));
  const position=p=>({x:50+p.x/maxX*390,y:65+p.y/maxY*125}),nodes=Object.fromEntries(points.map(p=>[p.id,p]));
  for(const [a,b] of map.edges||[]){if(!nodes[a]||!nodes[b])continue;const p=position(nodes[a]),q=position(nodes[b]);element('line',{x1:p.x,y1:p.y,x2:q.x,y2:q.y,stroke:'#9fb9ab','stroke-width':4});}
  for(const point of points){const p=position(point);element('circle',{cx:p.x,cy:p.y,r:8,fill:'#28745d'});element('text',{x:p.x-25,y:p.y+27,'font-size':12},point.name+' '+point.id);}
}catch(error){$('result').textContent=error.message;}}
$('mode').onchange=async()=>{const mode=$('mode').value;const result=await act('mode',{mode});if(!result){const actual=await api('mode');$('mode').value=actual.mode;}const indoor=$('mode').value==='indoor';$('indoorFields').hidden=!indoor;$('outdoorFields').hidden=indoor;if(indoor)await drawMap();};
$('plan').onsubmit=event=>{event.preventDefault();const mode=$('mode').value;act('nav/plan',mode==='indoor'?{mode,from:$('from').value,to:$('to').value}:{mode,origin:[Number($('lng').value),Number($('lat').value)],destination:$('destination').value});};
$('stop').onclick=()=>act('robot/stop');$('cancel').onclick=()=>act('nav/cancel');$('prepare').onclick=()=>act('robot/prepare');$('voice').onclick=()=>act('voice/text',{text:$('voiceText').value});
document.querySelectorAll('[data-action]').forEach(button=>button.onclick=()=>act('nav/'+button.dataset.action));document.querySelectorAll('[data-obstacle]').forEach(button=>button.onclick=()=>act('debug/obstacle',{state:button.dataset.obstacle}));
let eventLines=[];const ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws/app');ws.onopen=()=>$('wsState').textContent='已连接';ws.onclose=()=>$('wsState').textContent='已断开，请刷新页面';ws.onmessage=event=>{eventLines.unshift(event.data);eventLines=eventLines.slice(0,100);$('events').textContent=eventLines.join('\n');};
refresh();setInterval(refresh,2000);
