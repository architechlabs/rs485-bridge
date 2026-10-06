/* Profile-driven unit cards; state and controls are supplied by app.js. */
function unitCard(unit){
 const state=status.units[unit.id]||{}, p=profileFor(unit)||{points:[]}, values=state.values||{};
 const hasAddress=Number.isInteger(unit.address)&&unit.address>=0&&unit.address<=255;
 const roles=Object.fromEntries(p.points.filter(x=>x.role).map(x=>[x.role,x]));
 const allowed=config.tx_enabled&&config.allow_writes&&unit.control_enabled&&hasAddress;
 const read=role=>values[roles[role]?.id];
 const selector=(point,value)=>`<select data-control="${escape(point.id)}" data-unit="${escape(unit.id)}" ${allowed?'':'disabled'}><option value="" disabled ${value===undefined?'selected':''}>Waiting for read</option>${Object.keys(point.commands).map(o=>`<option value="${escape(o)}" ${value===o?'selected':''}>${escape(o.replace('_',' '))}</option>`).join('')}</select>`;
 const number=(point,value)=>`<input type="number" min="${point.minimum}" max="${point.maximum}" step="${point.step}" value="${escape(value??'')}" placeholder="—" data-control="${escape(point.id)}" data-unit="${escape(unit.id)}" ${allowed?'':'disabled'}>`;
 let controls='';
 for(const role of ['mode','fan','target']){
  const point=roles[role];if(!point)continue;
  controls+=`<div><label>${escape(point.name)}</label>${point.entity==='select'?selector(point,values[point.id]):number(point,values[point.id])}</div>`;
 }
 const points=p.points.map(point=>{
  const value=values[point.id], valid=state.point_availability?.[point.id];
  let control='';
  if(!point.role&&point.write_function){
   if(point.entity==='number')control=number(point,value);
   else if(point.entity==='select')control=selector(point,value);
   else control=Object.keys(point.commands).map(label=>`<button type="button" class="button small" data-point-action="${escape(point.id)}" data-unit="${escape(unit.id)}" data-value="${escape(label)}" ${allowed?'':'disabled'}>${escape(label)}</button>`).join(' ');
  }
  const error=state.point_errors?.[point.id];
  return `<div class="point-row"><span>${escape(point.name)}<small>${valid?'Verified read':value===undefined?'Waiting for read':'Cached / unavailable'}</small></span><strong>${escape(value??'—')} ${escape(point.unit||'')}</strong>${control?`<div class="controls">${control}</div>`:''}${error?`<p class="error-text">FC${String(error.function).padStart(2,'0')} · address ${error.address}: ${escape(error.error)}<br>${escape(error.hint)}</p>`:''}</div>`;
 }).join('');
 const assigned=hasAddress?unit.address.toString(16).toUpperCase().padStart(2,'0'):'UNASSIGNED';
 const phase=!hasAddress?'Address needed':state.available?'Verified':state.phase==='partial'?'Partial data':'Unavailable';
 const command=state.last_command;const feedback=command?`<div class="command-feedback">${escape(command.command_status)} · ${escape(command.point)}${command.command_latency_ms!==undefined?' · '+escape(command.command_latency_ms)+' ms':''}</div>`:'';
 const temperatures=roles.current||roles.target?`<div class="temperature"><div><strong>${escape(read('current')??'—')}<span> °C</span></strong><small>Reported room temperature</small></div><div><span class="setpoint">${escape(read('target')??'—')}°</span><small>Target</small></div></div>`:'';
 return `<article class="card"><div class="card-top"><h3>${escape(unit.name)}</h3><span class="badge ${state.available?'online':''}">${phase}</span></div><p class="card-meta">${unit.group?escape(unit.group)+' · ':''}${escape(unit.gateway)} · Unit ID ${unit.slave_id} · Indoor address ${assigned}</p>${temperatures}${feedback}<div class="controls">${controls}</div>${state.error?`<p class="error-text">${escape(state.error)}</p>`:''}${unit.address===null?'<p class="profile-notes">Select this room in LG Control → Info → Address, then enter its address in Edit. No requests will be sent to this room until assigned.</p>':''}<details class="point-details"><summary>All ${p.points.length} profile points & diagnostics</summary>${points}</details><div class="card-bottom"><span>${read('power')==='ON'?'● On':read('power')==='OFF'?'○ Off':'Awaiting state'} · ${allowed?'Control enabled':'Control locked'}</span>${roles.power?`<button class="button small" data-power="${escape(unit.id)}" data-value="${read('power')==='ON'?'OFF':'ON'}" ${allowed?'':'disabled'}>${read('power')==='ON'?'Turn off':'Turn on'}</button>`:''}<button class="button small" data-edit-unit="${escape(unit.id)}">Edit</button></div></article>`;
}
