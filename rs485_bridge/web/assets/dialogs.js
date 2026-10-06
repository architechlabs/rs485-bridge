/* HTML dialogs work inside iframe panels without native confirm()/prompt(). */
let activeDecision=null;
function showDecision(message,tokenInput=false){
 if(activeDecision)return Promise.resolve(tokenInput?null:false);
 return new Promise(resolve=>{
  const dialog=document.createElement('dialog');dialog.id='decision-dialog';
  const title=document.createElement('h2');title.textContent=tokenInput?'Local API authentication':'Review action';
  const text=document.createElement('p');text.textContent=message;text.className='decision-message';
  dialog.append(title,text);
  let input;
  if(tokenInput){input=document.createElement('input');input.type='password';input.autocomplete='off';input.id='decision-token';input.setAttribute('aria-label','API token');dialog.append(input);}
  const footer=document.createElement('div');footer.className='modal-foot';
  const cancel=document.createElement('button');cancel.className='button';cancel.textContent='Cancel';cancel.id='decision-cancel';
  const accept=document.createElement('button');accept.className='button primary';accept.textContent=tokenInput?'Connect':'Confirm';accept.id='decision-confirm';
  footer.append(cancel,accept);dialog.append(footer);document.body.append(dialog);activeDecision=dialog;
  const finish=value=>{dialog.close();dialog.remove();activeDecision=null;resolve(value);};
  cancel.onclick=()=>finish(tokenInput?null:false);
  accept.onclick=()=>finish(tokenInput?(input.value.trim()||null):true);
  dialog.addEventListener('cancel',event=>{event.preventDefault();finish(tokenInput?null:false)});
  dialog.showModal();if(tokenInput)input.focus();else cancel.focus();
 });
}
const uiConfirm=message=>showDecision(message,false);
const uiToken=message=>showDecision(message,true);
