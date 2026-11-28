/* A/B design test: counts what people do in each design, so we can compare A and B.
   Privacy: no cookies, no personal data, no job details. Only four event names per design:
   visit, open (opened a job), apply (pressed Apply), save (saved a job).
   Preview links (?v=a, ?v=b) are never counted, so our own checks don't skew the result.
   Counting is off until GOATCOUNTER is set (free, cookie-free analytics: https://www.goatcounter.com). */
(() => {
  const GOATCOUNTER = "";   // e.g. "workradar" for https://workradar.goatcounter.com
  const ab = window.WR_AB || {};
  if (!GOATCOUNTER || ab.how !== "random" || !/^[ab]$/.test(ab.v)) return;

  const queue = [];
  const send = name => {
    const path = `ab-${ab.v}-${name}`;
    if (window.goatcounter && window.goatcounter.count) window.goatcounter.count({ path, title: path, event: true });
    else queue.push(path);
  };
  window.goatcounter = { no_onload: true, allow_local: false };
  const s = document.createElement("script");
  s.async = true; s.src = "https://gc.zgo.at/count.js";
  s.dataset.goatcounter = `https://${GOATCOUNTER}.goatcounter.com/count`;
  s.onload = () => { while (queue.length) { const path = queue.shift(); window.goatcounter.count({ path, title: path, event: true }); } };
  document.head.appendChild(s);

  try { if (!sessionStorage.getItem("wr_ab_visit")) { sessionStorage.setItem("wr_ab_visit", "1"); send("visit"); } } catch (e) { send("visit"); }

  // Capture phase, so this runs before the page's own click handling changes aria-expanded.
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-expand], [data-apply], [data-save]"); if (!b) return;
    if (b.dataset.expand && b.getAttribute("aria-expanded") !== "true") send("open");
    else if (b.dataset.apply) send("apply");
    else if (b.dataset.save && b.getAttribute("aria-pressed") !== "true") send("save");
  }, true);
})();
