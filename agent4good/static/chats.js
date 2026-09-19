'use strict';
// Drafts stay in memory, scoped to a conversation, and disappear on sign-out.
const chatDrafts = new Map(), answerDrafts = new Map();
let chatSelected = '', chatEpoch = 0, chatBusy = false, chatData = null, chatPoll = false;
const chatClosed = s => ['done','failed','cancelled'].includes(s);
function chatRoute() { return location.hash.startsWith('#chats') ? location.hash.split('/')[1] || '' : ''; }
function clearChats() { chatEpoch++; chatSelected=''; chatData=null; chatDrafts.clear();answerDrafts.clear(); }
async function mountChats() {
  const selected=chatRoute();
  if ($('#chat-layout') && chatSelected===selected) { await refreshChat(); return; }
  chatEpoch++; chatSelected=selected; chatData=null;
  $('#main').innerHTML = heading('CONVERSATIONS','Talk through the work.','One conversation for each task. Pick up where you left off.') + `<section id="chat-layout" class="chat-layout"><aside class="chat-threads" aria-label="Task conversations"><div class="chat-list-head"><h2>Your chats</h2><button class="secondary" data-new-chat>New task</button></div><label class="sr-only" for="chat-search">Find a conversation</label><input id="chat-search" type="search" placeholder="Find a conversation"><div id="chat-list"></div></aside><section class="chat-conversation" aria-label="Conversation"><div id="chat-header"></div><div id="chat-messages" class="chat-messages" role="log" aria-label="Saved messages" aria-live="polite"></div><div id="chat-questions"></div><form id="chat-compose" hidden><label for="chat-content">Your message</label><textarea id="chat-content" name="content" rows="3" required maxlength="10000" placeholder="Ask about this task, or describe the next step."></textarea><div class="chat-compose-actions"><label for="chat-mode">Send as <select id="chat-mode" name="mode"><option value="ask">Ask a question</option><option value="work">Do work</option></select></label><button class="primary" type="submit">Send message ↗</button></div><p class="small muted">Questions use saved task evidence. Do work starts a new run with the same approval rules.</p><p id="chat-error" class="error" role="alert"></p></form><p id="chat-connection" class="small muted" role="status"></p></section></section>`;
  const draft=chatDrafts.get(selected);
  if(draft){$('#chat-content').value=draft.content;$('#chat-mode').value=draft.mode;}
  await refreshChat();
}
async function refreshChat() {
  if(current!=='chats'||!csrf||chatPoll)return;
  chatPoll=true;
  const epoch=chatEpoch, selected=chatSelected;
  try {
    const [rows,data]=await Promise.all([api('chats'),selected?api('tasks/'+encodeURIComponent(selected)+'/chat'):Promise.resolve(null)]);
    if(epoch!==chatEpoch||current!=='chats'||!$('#chat-layout'))return;
    const search=$('#chat-search').value.toLowerCase();
    $('#chat-list').innerHTML=rows.filter(t=>t.title.toLowerCase().includes(search)).map(t=>`<a class="chat-thread ${t.id===selected?'selected':''}" href="#chats/${encodeURIComponent(t.id)}" ${t.id===selected?'aria-current="true"':''}><strong>${escapeHTML(t.title)}</strong><span>${escapeHTML(pretty(t.status))} · ${time(t.last_message_at)}</span></a>`).join('') || '<p class="muted small">No matching conversations.</p>';
    if(!data){$('#chat-messages').innerHTML=empty('A conversation for every task.','Choose a task on the left, or start a new one.');return;}
    chatData=data;
    if(data.root_id!==selected){location.hash='chats/'+data.root_id;return;}
    const latest=data.runs.at(-1), active=data.runs.filter(r=>!chatClosed(r.status));
    $('#chat-header').innerHTML=`<div class="chat-title"><h2>${escapeHTML(data.title)}</h2><button class="text-link" data-task="${escapeHTML(latest.task_id)}">Work and decisions ↗</button></div><div class="chat-status">${badge(latest.status)}<span>${latest.status==='waiting_input'?'Your answer will resume this task.':latest.status==='waiting_approval'?'Open work and decisions to review the exact action.':latest.status==='queued'?'Your message is saved. The worker will reply here.':latest.status==='running'?'Your agent is working. This view updates automatically.':latest.status==='draft'?'Start this draft from Work and decisions.':'History is saved in your workspace.'}</span>${active.map(r=>r.status==='draft'?'':`<button class="text-link" data-chat-stop="${escapeHTML(r.task_id)}">Stop ${r.mode==='ask'?'reply':'work'}</button>`).join('')}</div><details class="chat-events"><summary>Recent progress</summary>${events(latest.events || [])}</details>`;
    const box=$('#chat-messages'), atBottom=box.scrollHeight-box.scrollTop-box.clientHeight<80;
    const html=data.messages.map(m=>`<article class="chat-message chat-${m.role}" data-message-id="${escapeHTML(m.id)}"><div class="chat-message-meta"><strong>${m.role==='user'?'You':m.role==='assistant'?'Agent4Good':'Run update'}</strong><time>${time(m.created_at)}</time></div><div class="chat-message-body">${escapeHTML(m.content)}</div>${m.needs_answer?'<p class="chat-question-label">Waiting for your answer below</p>':''}</article>`).join('');
    if(box.innerHTML!==html){box.innerHTML=html;if(atBottom)box.scrollTop=box.scrollHeight;}
    const questions=data.messages.filter(m=>m.needs_answer), questionKey=questions.map(q=>q.id).join(',');
    if($('#chat-questions').dataset.key!==questionKey){
      $('#chat-questions').dataset.key=questionKey;
      $('#chat-questions').innerHTML=questions.map(q=>`<form class="chat-answer" data-question="${escapeHTML(q.question_id)}" data-run="${escapeHTML(q.task_id)}"><label for="answer-${escapeHTML(q.id)}">${escapeHTML(q.content)}</label><textarea id="answer-${escapeHTML(q.id)}" name="content" required maxlength="10000" rows="2" placeholder="Your answer. Never paste credentials.">${escapeHTML(answerDrafts.get(q.id)||'')}</textarea><button class="primary" type="submit">Answer and resume</button><p class="error" role="alert"></p></form>`).join('');
    }
    $('#chat-compose').hidden=questions.length>0;
    const busyFollowup=data.runs.slice(1).some(r=>!chatClosed(r.status));
    $('#chat-compose button').disabled=chatBusy||busyFollowup;
    $('#chat-connection').textContent=busyFollowup&&!questions.length?'Waiting for the current reply. You can write your next message while it runs.':'Saved history · updates every 3 seconds';
  }catch(e){if(epoch===chatEpoch&&$('#chat-connection'))$('#chat-connection').textContent=e.message+' Your draft is still here. Retrying.';}
  finally{chatPoll=false;if(epoch!==chatEpoch&&current==='chats')void refreshChat();}
}
document.addEventListener('input',e=>{
  if(e.target.id==='chat-search')void refreshChat();
  if(e.target.closest('#chat-compose'))saveChatDraft();
  const answer=e.target.closest('.chat-answer');if(answer)answerDrafts.set(answer.dataset.question,e.target.value);
});
document.addEventListener('change',e=>{if(e.target.id==='chat-mode')saveChatDraft();});
function saveChatDraft(){const old=chatDrafts.get(chatSelected),content=$('#chat-content').value,mode=$('#chat-mode').value;chatDrafts.set(chatSelected,{content,mode,request_id:old?.content===content&&old?.mode===mode?old.request_id:crypto.randomUUID()});}
document.addEventListener('click',async e=>{
  const button=e.target.closest('button');if(!button)return;
  if(button.hasAttribute('data-new-chat')){newTask();$('#task-form').dataset.chat='true';}
  if(button.dataset.chat){$('#modal').close();location.hash='chats/'+button.dataset.chat;}
  if(button.dataset.chatStop){button.disabled=true;try{await mutate('tasks/'+button.dataset.chatStop+'/cancel');await refreshChat();}catch(err){toast(err.message);button.disabled=false;}}
});
document.addEventListener('submit',async e=>{
  const form=e.target;if(form.id!=='chat-compose'&&!form.classList.contains('chat-answer'))return;
  e.preventDefault();if(chatBusy)return;
  const selected=chatSelected,epoch=chatEpoch,button=e.submitter;
  chatBusy=true;button.disabled=true;
  const error=form.querySelector('.error');error.textContent='';
  try{
    if(form.id==='chat-compose'){
      saveChatDraft();const draft=chatDrafts.get(selected);
      await mutate('tasks/'+encodeURIComponent(selected)+'/chat',draft);
      // A late response must never clear the draft of a different thread.
      if(chatDrafts.get(selected)?.request_id===draft.request_id){chatDrafts.delete(selected);if(epoch===chatEpoch)form.reset();}
    }else{
      await mutate('tasks/'+form.dataset.run+'/questions/'+form.dataset.question+'/answer',{content:new FormData(form).get('content')});
      answerDrafts.delete(form.dataset.question);
    }
    await refreshChat();
  }catch(err){if(epoch===chatEpoch)error.textContent=err.message;else toast(err.message);}
  finally{chatBusy=false;if(button.isConnected)button.disabled=false;void refreshChat();}
});
setInterval(()=>{if(csrf&&current==='chats'&&!document.hidden)void refreshChat();},3000);
