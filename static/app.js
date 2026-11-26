(function () {
  document.querySelectorAll("[data-composition-form]").forEach(function (form) {
    var select = form.querySelector("[data-template-select]");
    if (!select) return;
    function sync() {
      form.querySelectorAll("[data-template-fields]").forEach(function (group) {
        group.hidden = group.getAttribute("data-template-fields") !== select.value;
      });
    }
    select.addEventListener("change", sync);
    sync();
  });

  document.querySelectorAll("[data-ask-form]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var result = form.parentElement.querySelector("[data-ask-result]");
      result.hidden = false;
      result.textContent = "正在生成…";
      var body = new URLSearchParams();
      body.append("patient_id", form.getAttribute("data-patient-id"));
      body.append("question", form.querySelector("[name=question]").value);
      fetch("/api/ask", { method: "POST", body: body })
        .then(function (response) { return response.json(); })
        .then(function (data) { result.textContent = data.answer || data.error || "无结果"; })
        .catch(function () { result.textContent = "请求失败"; });
    });
  });
})();
