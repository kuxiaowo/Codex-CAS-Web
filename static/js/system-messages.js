(() => {
  const container=document.querySelector('[data-system-notifications]');
  if(!container) return;
  let page=1;
  const more=document.querySelector('[data-system-more]');
  async function load(append=false){
    try{
      const result=await window.NetHubModeration.api(`/system-notifications?page=${page}`);
      window.NetHubModeration.renderNotifications(container,result,append);
      more.hidden=!result.hasMore;
      try {
        await window.NetHubModeration.api('/system-notifications/read',{method:'POST',body:JSON.stringify({throughId:result.latestId})});
      } catch { /* Keep loaded cards visible if marking read temporarily fails. */ }
      window.dispatchEvent(new Event('casSystemMessagesRead'));
    }catch(error){container.textContent=error.message;}
  }
  more.onclick=()=>{page++;load(true);};load();
  for(const kind of ['reply','like']){
    const target=document.querySelector(`[data-comment-notifications="${kind}"]`);
    const button=document.querySelector(`[data-comment-more="${kind}"]`);
    let current=1;
    async function loadKind(append=false){
      try{
        const result=await window.CASNotes.api(`/api/comment-notifications?kind=${kind}&page=${current}`);
        if(!append)target.replaceChildren();
        for(const item of result.data){
          const card=document.createElement('article');card.className=`mod-card mod-notification ${item.read?'':'unread'}`;
          const heading=document.createElement('strong');heading.textContent=`${item.actor}${kind==='reply'?'回复了你':'赞了你的留言'}`;
          const detail=document.createElement('p');detail.textContent=item.available?item.content:'相关留言已隐藏、删除或原图集不可访问';
          card.append(heading,detail);
          if(item.url){const link=document.createElement('a');link.href=item.url;link.textContent=`查看「${item.targetTitle}」`;card.append(link);}
          target.append(card);
        }
        if(!target.childElementCount)target.textContent='暂无消息';
        button.hidden=!result.hasMore;
        await window.CASNotes.api('/api/comment-notifications/read',{method:'POST',body:JSON.stringify({kind,throughId:result.latestId})});
        window.dispatchEvent(new Event('casSystemMessagesRead'));
      }catch(error){target.textContent=error.message;}
    }
    button.onclick=()=>{current++;loadKind(true);};loadKind();
  }
})();
