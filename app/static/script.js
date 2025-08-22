document.addEventListener('DOMContentLoaded', () => {
  // Remember sidebar tree open/closed state
  document.querySelectorAll('[data-tree-node]').forEach((det, idx) => {
    const key = 'tree_open_'+idx;
    const saved = localStorage.getItem(key);
    if(saved === '1') det.setAttribute('open','');
    if(saved === '0') det.removeAttribute('open');
    det.addEventListener('toggle', () => {
      localStorage.setItem(key, det.open ? '1' : '0');
    });
  });

  // Build a simple TOC for article pages
  const article = document.querySelector('article.prose');
  if(article){
    const headings = article.querySelectorAll('h1, h2, h3');
    if(headings.length > 2){
      const toc = document.createElement('nav');
      toc.className = 'toc';
      const ul = document.createElement('ul');
      headings.forEach((h, i) => {
        const id = h.id || ('h_' + i);
        h.id = id;
        const li = document.createElement('li');
        li.style.margin = '.2rem 0';
        const a = document.createElement('a');
        a.href = '#'+id;
        a.textContent = (h.textContent || '').trim();
        a.style.textDecoration = 'none';
        a.style.color = 'var(--primary)';
        li.appendChild(a);
        ul.appendChild(li);
      });
      toc.appendChild(ul);
      article.parentElement.insertBefore(toc, article);
    }
  }
});
