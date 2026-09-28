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
})();
