/* Hover to enlarge a scan or its catalogue reference.
 *
 * Printings are separated by a set symbol a few pixels wide, so the review
 * queue's whole job is comparing two images closely — and it renders them at
 * 46x64. This puts the full-size versions under the pointer without a click,
 * a modal, or a page of its own.
 *
 * One preview element for the page, positioned `fixed`, because every
 * thumbnail sits inside a pane with `overflow: auto` and an absolutely
 * positioned child of a scrolling container gets clipped at its edge.
 */
(function () {
  const box = document.getElementById('zoom');
  const img = document.getElementById('zoom-img');
  const cap = document.getElementById('zoom-cap');
  if (!box || !img) return;

  // Matches both the queue thumbnails and the smaller inventory ones.
  const SELECTOR = '.thumb img, .thumb-xs img';
  const GAP = 14;

  let current = null;

  function label(el) {
    const holder = el.closest('.thumb, .thumb-xs');
    const isRef = holder && holder.classList.contains('ref');
    const row = el.closest('.qrow, tr');
    let name = '';
    if (row) {
      const q = row.querySelector('.qname, .name');
      if (q) name = q.textContent.trim();
    }
    const side = isRef ? 'catalogue' : 'your scan';
    return name ? side + ' · ' + name : side;
  }

  function place(el) {
    const r = el.getBoundingClientRect();
    // Measure after the image is in place, so the height used is the real one.
    const w = box.offsetWidth || 330;
    const h = box.offsetHeight || 460;

    // Prefer the right of the thumbnail; flip left when that would overflow.
    let x = r.right + GAP;
    if (x + w > window.innerWidth - 8) x = r.left - w - GAP;
    if (x < 8) x = 8;

    // Vertically centred on the thumbnail, clamped into the viewport so a row
    // at the very top or bottom still shows the whole card.
    let y = r.top + r.height / 2 - h / 2;
    y = Math.max(8, Math.min(y, window.innerHeight - h - 8));

    box.style.left = Math.round(x) + 'px';
    box.style.top = Math.round(y) + 'px';
  }

  function show(el) {
    if (current === el) return;
    current = el;
    cap.textContent = label(el);
    // Same URL the thumbnail already used, so it is usually in cache and the
    // preview appears instantly rather than flashing the previous card.
    if (img.getAttribute('src') !== el.currentSrc && img.src !== el.src) {
      img.src = el.currentSrc || el.src;
    }
    box.classList.add('on');
    place(el);
    // Re-place once the image has its natural height, otherwise the first
    // hover of a not-yet-loaded card is centred against a zero-height box.
    if (!img.complete) img.addEventListener('load', () => current === el && place(el), { once: true });
  }

  function hide() {
    current = null;
    box.classList.remove('on');
  }

  // Delegated, so rows added or replaced by a reload need no rebinding.
  document.addEventListener('mouseover', (ev) => {
    const el = ev.target.closest(SELECTOR);
    if (el) show(el); else if (current) hide();
  });
  document.addEventListener('mouseout', (ev) => {
    if (ev.target.closest(SELECTOR) && !ev.relatedTarget?.closest(SELECTOR)) hide();
  });
  // A thumbnail can slide out from under a stationary pointer.
  window.addEventListener('scroll', hide, true);
  window.addEventListener('resize', hide);
  // Clicking a row toggles selection; a preview left hanging over the new
  // state is just in the way.
  document.addEventListener('click', hide);
  document.addEventListener('keydown', (ev) => ev.key === 'Escape' && hide());
})();
