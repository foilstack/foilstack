/* Helpers shared by more than one screen.
 *
 * This file exists because the catalogue-search widget was written twice —
 * once for the review queue and once for the inventory drawer — from the same
 * server-rendered markup. Both copies had their own debounce and their own
 * guard against out-of-order responses, which meant a fix to either was a fix
 * to one screen, and the two had already started to drift.
 *
 * Plain globals rather than ES modules. Modules would need `type="module"` on
 * every page script and would then load deferred, after the inline scripts
 * that call these; the alternative is a bundler, and not having a build step
 * is a feature of a project people self-host. Four names is a budget, not a
 * pattern to extend — anything page-specific belongs on its page.
 */

/* eslint-disable no-unused-vars */

const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

/* How long to wait after a keystroke before searching. Every character is a
 * substring scan over the whole catalogue, so this is not free: 200ms is below
 * the point a search box starts to feel laggy and comfortably above typing
 * speed. */
const SEARCH_DEBOUNCE_MS = 200;

/* Read the server's complaint out of a failed response.
 *
 * Wrapped in a try because the previous inline version was `(await
 * res.json()).detail`, which assumes every failure is a FastAPI validation
 * error. A 502 from a proxy, or any HTML error page, made that throw inside an
 * async handler — so the user got no alert at all and the real status vanished
 * into an unhandled rejection. The fallback is worth more than the detail.
 */
async function errorDetail(res, fallback) {
  try {
    return (await res.json()).detail || fallback;
  } catch (e) {
    return fallback;
  }
}

/* POST a JSON body and hand back the parsed reply, or null if it failed.
 *
 * Returning null rather than throwing keeps the call sites in their existing
 * shape — `if (await postJSON(...)) location.reload()` — and the alert happens
 * here so that eight endpoints cannot report failure eight different ways.
 * Pass `quiet` when the caller shows the failure itself, as the listings
 * screen does by re-enabling its button.
 */
async function postJSON(url, body, opts) {
  opts = opts || {};
  const res = await fetch(url, {
    method: opts.method || 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    if (!opts.quiet) alert(await errorDetail(res, opts.error || 'that did not save'));
    return null;
  }
  // 204s and empty 200s are legitimate here; an empty body is a success with
  // nothing to say, not a parse error.
  return await res.json().catch(() => ({}));
}

/* The form-encoded counterpart, for the scan endpoints that take Form(...)
 * parameters rather than a JSON model. Same contract, so a caller does not
 * have to care which kind of endpoint it is talking to.
 */
async function postForm(url, fields, opts) {
  opts = opts || {};
  const res = await fetch(url, {
    method: 'POST',
    body: fields ? new URLSearchParams(fields) : undefined,
  });
  if (!res.ok) {
    if (!opts.quiet) alert(await errorDetail(res, opts.error || 'that did not save'));
    return null;
  }
  return await res.json().catch(() => ({}));
}

/* Wire up a "pick the right card" panel.
 *
 * `box` is any element containing the three data attributes the server
 * renders in _card_search.html: the query input, the game filter and the
 * results container. Both screens that correct a bad match use it — the
 * review queue re-points a scan, the inventory drawer re-points a saved row —
 * and they differ only in what happens on a pick, which is the callback.
 *
 * Returns the search function so a caller can re-run it; ignoring the return
 * is fine, since it runs once on wiring.
 */
function wireCardSearch(box, opts) {
  const q = $('[data-fix-q]', box);
  const game = $('[data-fix-game]', box);
  const out = $('[data-fix-results]', box);
  if (!q || !game || !out) return null;

  let timer = null;
  let seq = 0;

  async function run() {
    // A slow reply for "gok" can land after a fast one for "goku" and paint
    // the wrong results over the right ones. Only the newest request is
    // allowed to write, which is cheaper and more reliable than cancelling.
    const mine = ++seq;
    const url =
      '/api/cards/search?q=' + encodeURIComponent(q.value) +
      '&game=' + encodeURIComponent(game.value);
    const res = await fetch(url);
    if (!res.ok || mine !== seq) return;
    out.innerHTML = await res.text();
  }

  q.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(run, SEARCH_DEBOUNCE_MS);
  });
  game.addEventListener('change', run);

  // Delegated from the box rather than the results list, so the scan's own
  // runners-up — rendered above the search, from the same partial — are
  // clickable through the same path.
  box.addEventListener('click', ev => {
    const pick = ev.target.closest('[data-pick]');
    if (!pick) return;
    // Re-pointing a saved inventory row is worth a confirmation; re-pointing
    // a queued scan that nobody has committed yet is not. The caller decides
    // by returning a question, or nothing.
    const ask = opts.confirmPick && opts.confirmPick(pick);
    if (ask && !confirm(ask)) return;
    opts.onPick(pick);
  });

  // The panel opens with the card's current name already in the box, so there
  // is something worth showing before the first keystroke.
  run();
  return run;
}
