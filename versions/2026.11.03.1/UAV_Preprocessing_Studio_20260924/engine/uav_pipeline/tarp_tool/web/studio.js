fetch('/context').then(r=>r.json()).then(j=>{
  document.title=j.label+' | 타프 ROI';
  document.querySelector('h1').textContent=j.label+' | 타프 위치·ROI';
  const note=document.createElement('span');
  note.textContent=j.mosaic_role==='same_flight'?'해당 회차 모자이크':'다른 회차·날짜 모자이크 · 위치 참조 전용';
  document.querySelector('header').append(note);
  const source=document.createElement('span');source.textContent=j.candidate_preview_kind==='panel_photos'?'Panel Photos 후보 확인 · 선택 번호는 원영상과 연결':'방사보정 radiance · 센서교정 상태는 별도 확인';document.querySelector('header').append(source);
  if(location.pathname==='/roi.html'){
    names.splice(0,names.length,...j.tarps);buttons();
  }
}).catch(e=>document.getElementById('status').textContent=e.message);
if(document.getElementById('band')){
  const unsupported=document.querySelector('input[value="automatic_proposal"]');
  if(unsupported)unsupported.closest('label').hidden=true;
  fetch('/band_labels').then(r=>r.json()).then(labels=>{
    if(!Array.isArray(labels))return;
    const select=document.getElementById('band'),value=select.value;
    select.replaceChildren(...labels.map((s,i)=>new Option(s,i+1)));select.value=value;
  });
  document.getElementById('mosaic').onerror=()=>document.getElementById('status').textContent='모자이크 읽기 실패 · 좌표계·파일 확인 필요';
}
if(location.pathname==='/roi.html'){
  im.onerror=()=>document.getElementById('status').textContent='선택 영상 RGB 정합 실패 · 입력 모드와 server.log 확인';
}
