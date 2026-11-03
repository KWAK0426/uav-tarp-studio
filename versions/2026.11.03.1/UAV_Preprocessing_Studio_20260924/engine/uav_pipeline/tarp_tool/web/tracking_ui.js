const trackingPanel=document.createElement('section');
trackingPanel.style.cssText='border-top:1px solid #bbc9c5;margin-top:18px;padding-top:14px';
trackingPanel.innerHTML='<h2 style="font-size:16px;margin:0 0 10px">남은 영상 자동 추적</h2><p id="referenceCount">저장된 기준 ROI 확인 중</p><button id="trackRun" disabled>남은 영상 자동 추적</button><progress id="trackProgress" max="1" value="0" style="display:block;width:100%;margin-top:12px"></progress><p id="trackState">대기</p><a id="trackReport" hidden>추적 결과 검수 →</a>';
document.querySelector('aside').append(trackingPanel);
const aside=document.querySelector('aside'),editControls=document.createElement('div');
editControls.style.cssText='flex:1;min-height:0;overflow:auto;padding-right:5px';
Array.from(aside.children).filter(e=>e!==trackingPanel).forEach(e=>editControls.append(e));
aside.prepend(editControls);aside.style.cssText='display:flex;flex-direction:column;overflow:hidden;min-height:0';
trackingPanel.style.flexShrink='0';trackingPanel.style.marginTop='8px';
let savedVersion=JSON.stringify(tarps),jobRunning=false;
const originalLoad=load;
load=function(){const saved=data?.references?.[$('ref').value];if(saved&&(reference!==$('ref').value||!Object.keys(tarps).length))tarps=JSON.parse(JSON.stringify(saved));originalLoad();savedVersion=JSON.stringify(tarps)};
$('ref').onchange=()=>{if(JSON.stringify(tarps)!==savedVersion&&!confirm('저장하지 않은 변경이 있습니다. 영상 변경을 계속할까요?')){$('ref').value=reference;return;}tarps=JSON.parse(JSON.stringify(data.references?.[$('ref').value]||{}));load()};
function readyReferences(){return Object.entries(data?.references||{}).filter(([s,t])=>data.selected.some(r=>r.shot===s)&&names.every(n=>t[n]?.points?.length===4&&t[n].status!=='failed'))}
function updateReady(){if(!data)return;const refs=readyReferences();$('referenceCount').textContent=`저장된 기준 ${refs.length}장: ${refs.map(([s])=>s).join(', ')||'없음'} / 선택 ${data.selected.length}장`;$('trackRun').disabled=jobRunning||refs.length<2||data.selected.length<2;for(const o of $('ref').options)o.textContent=o.value+(refs.some(([s])=>s===o.value)?' · 기준 ROI 저장됨':'')}
const originalSave=$('save').onclick;
$('save').onclick=async()=>{await originalSave();const r=await fetch('/selection/latest',{cache:'no-store'}),j=await r.json();if(r.ok&&j.selection_file===data.selection_file){data.references=j.references;const saved=data.references?.[reference];if(saved&&JSON.stringify(saved)===JSON.stringify(tarps))savedVersion=JSON.stringify(tarps);updateReady()}};
$('trackRun').onclick=async()=>{if(JSON.stringify(tarps)!==savedVersion){$('trackState').textContent='현재 영상의 변경 내용을 먼저 저장해주세요.';return}if(!confirm('저장된 기준 ROI를 사용해 나머지 영상을 자동 추적할까요? 결과는 검수 전 상태로 별도 저장되며 기준 ROI는 변경하지 않습니다.'))return;try{$('trackRun').disabled=true;const r=await fetch('/tracking/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selection_file:data.selection_file})}),j=await r.json();if(!r.ok)throw Error(j.error);showJob(j)}catch(e){$('trackState').textContent=e.message;updateReady()}};
function showJob(j){jobRunning=j.status==='running';$('trackProgress').max=j.total||1;$('trackProgress').value=j.done||0;$('trackState').textContent=j.message+(j.total?` · ${j.done}/${j.total} (${Math.round(j.done/j.total*100)}%)`:'');$('trackReport').hidden=!j.report;if(j.report)$('trackReport').href=j.report;updateReady()}
async function pollTracking(){try{if(data){updateReady();const r=await fetch('/tracking/status',{cache:'no-store'});if(!r.ok)throw Error('추적 상태 연결 실패');showJob(await r.json())}}catch(e){$('trackState').textContent=e.message}finally{setTimeout(pollTracking,2000)}}
pollTracking();
const cancelButton=document.createElement('button');cancelButton.textContent='현재 추적 취소';cancelButton.hidden=true;trackingPanel.append(cancelButton);
let activeJob=null;const baseShow=showJob;
showJob=function(j){activeJob=j.job_id;baseShow(j);cancelButton.hidden=!jobRunning;for(const e of editControls.querySelectorAll('button,input,select'))e.disabled=jobRunning;};
cancelButton.onclick=async()=>{try{const r=await fetch('/tracking/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_id:activeJob})});const j=await r.json();if(!r.ok)throw Error(j.error);showJob(j);}catch(e){$('trackState').textContent=e.message;}};
