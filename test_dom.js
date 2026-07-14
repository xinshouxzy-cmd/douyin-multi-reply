// ===================================================
// 抖音私信页面 DOM 分析 — 粘贴到浏览器 Console 运行
// ===================================================
// 使用方法:
//   1. 打开 https://www.douyin.com/chat?isPopup=1
//   2. F12 打开开发者工具 → Console 标签
//   3. 粘贴下面全部代码 → 回车
//   4. 查看输出结果

(function() {
  console.log('=== 第一步：找到所有包含「陌生人消息」的元素 ===');
  
  // 1. 找所有叶子节点包含"陌生人消息"的
  const strangers = [];
  document.querySelectorAll('*').forEach(el => {
    if (el.children.length > 0) return;
    const t = (el.textContent || '').trim();
    if (t === '陌生人消息' || t === '陌生人') {
      // 找祖先可点击元素
      let p = el;
      let chain = [];
      for (let d = 0; d < 10 && p; d++) {
        const cls = (p.className || '').toString();
        const tag = p.tagName.toLowerCase();
        chain.push(tag + (cls ? '.' + cls.substring(0, 30) : ''));
        p = p.parentElement;
      }
      strangers.push({
        text: t,
        tag: el.tagName,
        class: (el.className || '').toString(),
        rect: el.getBoundingClientRect(),
        ancestorChain: chain.join(' > ')
      });
    }
  });
  
  console.table(strangers.map(s => ({
    '文本': s.text,
    '标签': s.tag,
    'x': Math.round(s.rect.x),
    'y': Math.round(s.rect.y),
    '宽': Math.round(s.rect.width),
    '高': Math.round(s.rect.height),
    '祖先链': s.ancestorChain.substring(0, 80)
  })));

  // 2. 如果有找到，尝试点击第一个
  if (strangers.length > 0) {
    console.log('\n=== 第二步：尝试点击 ===');
    const target = strangers[0];
    let el = document.elementFromPoint(
      target.rect.x + target.rect.width/2,
      target.rect.y + target.rect.height/2
    );
    
    console.log('点击目标:', {
      tag: el?.tagName,
      class: (el?.className || '').toString(),
      text: el?.textContent?.trim().substring(0, 50),
      rect: el?.getBoundingClientRect()
    });
    
    if (el) {
      el.scrollIntoView({block: 'center'});
      el.click();
      console.log('✅ 已点击，观察页面是否跳转到陌生人列表');
    }
  } else {
    console.log('❌ 没找到「陌生人消息」，可能当前没有陌生人');
  }
  
  console.log('\n=== 第三步：当前页面所有对话项 ===');
  const items = document.querySelectorAll('[class*="conversation"],[class*="ConversationItem"]');
  items.forEach((el, i) => {
    const rect = el.getBoundingClientRect();
    console.log(`  [${i}] ${(el.textContent||'').substring(0, 60)} | pos(${Math.round(rect.x)},${Math.round(rect.y)})`);
  });
  
  console.log('\n=== 第四步：检查返回按钮 ===');
  const backBtns = document.querySelectorAll('[class*="back"], [class*="Back"], [class*="return"], [class*="arrow"]');
  console.log('返回按钮数量:', backBtns.length);
  backBtns.forEach((b, i) => {
    const r = b.getBoundingClientRect();
    console.log(`  [${i}] tag=${b.tagName} class=${(b.className||'').substring(0,40)} pos(${Math.round(r.x)},${Math.round(r.y)}) size=${Math.round(r.width)}x${Math.round(r.height)}`);
  });
  
})();
