// 月需量重算：命中当前批次重新取清零周期最高值与兜底发布，成功后刷新页面。
const button = document.getElementById("demand-recalc-button");
if (button) {
  button.addEventListener("click", async () => {
    const noticeBox = document.getElementById("notice");
    function notice(message, isError = false) {
      if (!noticeBox) return;
      noticeBox.textContent = message;
      noticeBox.classList.toggle("is-error", Boolean(isError));
      noticeBox.hidden = false;
    }
    if (button.disabled) return;
    button.disabled = true;
    const label = button.textContent;
    button.textContent = "重算中";
    try {
      const response = await fetch("/api/demand/recalc", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error((payload.error || response.status) + " " + (payload.message || ""));
      }
      const stats = payload.stats || {};
      notice("已重算：" + (stats.demand_meter_months || 0) + " 个表计·月，"
        + (stats.provisional_count || 0) + " 个待追补");
      window.setTimeout(() => window.location.reload(), 500);
    } catch (error) {
      notice(String(error.message || error), true);
      button.disabled = false;
      button.textContent = label;
    }
  });
}
