/* =====================================================================
   KING Downloader — Mini App
   منطق: لینک → بررسی → کیفیت → دانلود → پیشرفت → نتیجه
   ===================================================================== */
(function () {
  "use strict";

  // ---------------------------------------------------------------
  // تلگرام
  // ---------------------------------------------------------------
  var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  var initData = tg ? tg.initData || "" : "";

  if (tg) {
    try {
      tg.ready();
      tg.expand();
      if (tg.setHeaderColor) tg.setHeaderColor("#07070f");
      if (tg.setBackgroundColor) tg.setBackgroundColor("#07070f");
      if (tg.disableVerticalSwipes) tg.disableVerticalSwipes();
    } catch (e) {
      /* نسخه‌های قدیمی تلگرام این متدها را ندارند */
    }
  }

  // ---------------------------------------------------------------
  // وضعیت
  // ---------------------------------------------------------------
  var state = {
    jobId: null,
    platform: "youtube",
    polling: null,
    busy: false
  };

  var POLL_MS = 900;

  // ---------------------------------------------------------------
  // عناصر
  // ---------------------------------------------------------------
  function $(id) { return document.getElementById(id); }

  var el = {
    greeting: $("greeting"),
    connDot: $("conn-dot"),

    stepLink: $("step-link"),
    stepVideo: $("step-video"),
    stepProgress: $("step-progress"),
    stepResult: $("step-result"),
    stepError: $("step-error"),

    urlInput: $("url-input"),
    clearBtn: $("clear-btn"),
    probeBtn: $("probe-btn"),
    linkHint: $("link-hint"),

    thumbImg: $("thumb-img"),
    videoTitle: $("video-title"),
    tagPlatform: $("tag-platform"),
    tagDuration: $("tag-duration"),
    qualities: $("qualities"),
    audioBtn: $("audio-btn"),
    backBtn: $("back-btn"),

    progressTitle: $("progress-title"),
    progressFill: $("progress-fill"),
    progressPercent: $("progress-percent"),
    progressSize: $("progress-size"),
    statSpeed: $("stat-speed"),
    statDone: $("stat-done"),
    statTotal: $("stat-total"),
    cancelBtn: $("cancel-btn"),

    resultIco: $("result-ico"),
    resultTitle: $("result-title"),
    resultText: $("result-text"),
    againBtn: $("again-btn"),

    errorTitle: $("error-title"),
    errorText: $("error-text"),
    errorBackBtn: $("error-back-btn")
  };

  // ---------------------------------------------------------------
  // ابزار
  // ---------------------------------------------------------------
  var FA_DIGITS = ["۰", "۱", "۲", "۳", "۴", "۵", "۶", "۷", "۸", "۹"];

  function faNum(value) {
    return String(value).replace(/[0-9]/g, function (d) { return FA_DIGITS[+d]; });
  }

  function show(step) {
    [el.stepLink, el.stepVideo, el.stepProgress, el.stepResult, el.stepError]
      .forEach(function (node) { node.hidden = true; });
    if (step) step.hidden = false;
  }

  function setConn(ok) {
    el.connDot.classList.toggle("is-online", ok === true);
    el.connDot.classList.toggle("is-offline", ok === false);
  }

  function setBusy(button, busy, label) {
    var spinner = button.querySelector(".spinner");
    var text = button.querySelector(".btn-label");
    button.disabled = busy;
    if (spinner) spinner.hidden = !busy;
    if (text && label) text.textContent = label;
  }

  function haptic(type) {
    if (!tg || !tg.HapticFeedback) return;
    try {
      if (type === "error") tg.HapticFeedback.notificationOccurred("error");
      else if (type === "success") tg.HapticFeedback.notificationOccurred("success");
      else tg.HapticFeedback.impactOccurred("light");
    } catch (e) { /* بی‌اهمیت */ }
  }

  // ---------------------------------------------------------------
  // درخواست به بک‌اند
  // ---------------------------------------------------------------
  function api(path, options) {
    options = options || {};
    var headers = { "Content-Type": "application/json" };
    if (initData) headers["X-Init-Data"] = initData;

    return fetch(path, {
      method: options.method || "GET",
      headers: headers,
      body: options.body ? JSON.stringify(options.body) : undefined
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) {
          var err = new Error(data.message || "خطای سرور");
          err.code = data.error || "http_" + response.status;
          err.status = response.status;
          throw err;
        }
        return data;
      });
    });
  }

  // ---------------------------------------------------------------
  // خطا
  // ---------------------------------------------------------------
  function showError(title, message) {
    stopPolling();
    el.errorTitle.textContent = title;
    el.errorText.textContent = message;
    show(el.stepError);
    haptic("error");
  }

  function handleError(err) {
    if (err && err.status === 401) {
      showError(
        "اعتبارسنجی ناموفق",
        "این صفحه باید از داخل تلگرام باز شود. ربات را باز کن و دکمه‌ی «باز کردن Mini App» را بزن."
      );
      return;
    }
    showError("خطا", (err && err.message) || "مشکلی پیش آمد. دوباره امتحان کن.");
  }

  // ---------------------------------------------------------------
  // مرحله ۱: بررسی لینک
  // ---------------------------------------------------------------
  function probe() {
    var url = el.urlInput.value.trim();
    if (!url) {
      el.linkHint.textContent = "اول لینک را بچسبان.";
      el.urlInput.focus();
      haptic("error");
      return;
    }
    if (!/^https?:\/\//i.test(url)) {
      el.linkHint.textContent = "لینک باید با http:// یا https:// شروع شود.";
      haptic("error");
      return;
    }

    setBusy(el.probeBtn, true, "در حال بررسی...");
    el.linkHint.textContent = "در حال گرفتن اطلاعات ویدیو...";

    api("/api/probe", { method: "POST", body: { url: url } })
      .then(function (job) {
        state.jobId = job.job_id;
        renderVideo(job);
        show(el.stepVideo);
        haptic("success");
      })
      .catch(handleError)
      .finally(function () {
        setBusy(el.probeBtn, false, "بررسی لینک");
        el.linkHint.textContent = "لینک ویدیو را از اپ پلتفرم کپی کن و اینجا بچسبان.";
      });
  }

  // ---------------------------------------------------------------
  // مرحله ۲: کارت ویدیو
  // ---------------------------------------------------------------
  var PLATFORM_FA = {
    youtube: "یوتیوب",
    instagram: "اینستاگرام",
    tiktok: "تیک‌تاک"
  };

  function renderVideo(job) {
    el.videoTitle.textContent = job.title || "ویدیو";
    el.tagPlatform.textContent = PLATFORM_FA[job.platform] || job.platform || "—";
    el.tagDuration.textContent = job.duration_text || "—";

    if (job.thumbnail) {
      el.thumbImg.src = job.thumbnail;
      el.thumbImg.hidden = false;
      el.thumbImg.onerror = function () { el.thumbImg.hidden = true; };
    } else {
      el.thumbImg.hidden = true;
    }

    renderQualities(job);
  }

  function renderQualities(job) {
    el.qualities.innerHTML = "";

    // فقط کیفیت‌هایی که در سقف ارسال تلگرام جا می‌شوند (سرور فیلتر کرده)
    var list = job.qualities;
    if (!list) {
      list = (job.heights || []).map(function (h) { return { height: h, size_text: "" }; });
    }
    var hasHeights = (job.heights || []).length > 0;

    if (hasHeights && list.length === 0) {
      var note = document.createElement("p");
      note.className = "hint";
      note.textContent = "حتی کمترین کیفیت از سقف ارسال تلگرام بزرگ‌تر است؛ فقط دانلود صدا ممکن است.";
      el.qualities.appendChild(note);
      return;
    }

    // «بهترین کیفیت» همیشه اول و تمام‌عرض
    var best = document.createElement("button");
    best.className = "q-btn is-best";
    var bestSub = job.best_height
      ? faNum(job.best_height) + " پیکسل — بالاترین کیفیتی که در تلگرام جا می‌شود"
      : "بالاترین کیفیت موجود";
    best.innerHTML =
      '<span class="q-label">⭐ بهترین کیفیت</span>' +
      '<span class="q-sub">' + bestSub + "</span>";
    best.addEventListener("click", function () { startDownload(null, false); });
    el.qualities.appendChild(best);

    // بقیه‌ی کیفیت‌ها، نزولی
    list.slice().sort(function (a, b) { return b.height - a.height; }).forEach(function (q) {
      var btn = document.createElement("button");
      btn.className = "q-btn";
      var sub = q.size_text ? "≈ " + q.size_text : faNum(q.height) + " پیکسل";
      btn.innerHTML =
        '<span class="q-label">🎞 ' + faNum(q.height) + "p</span>" +
        '<span class="q-sub">' + sub + "</span>";
      btn.addEventListener("click", function () { startDownload(q.height, false); });
      el.qualities.appendChild(btn);
    });
  }

  // ---------------------------------------------------------------
  // مرحله ۳: دانلود
  // ---------------------------------------------------------------
  function startDownload(height, audioOnly) {
    if (!state.jobId || state.busy) return;
    state.busy = true;

    show(el.stepProgress);
    el.progressTitle.textContent = audioOnly ? "در حال دانلود صدا..." : "در حال دانلود...";
    el.progressFill.style.width = "0%";
    el.progressPercent.textContent = "۰٪";
    el.progressSize.textContent = "—";
    el.statSpeed.textContent = "—";
    el.statDone.textContent = "—";
    el.statTotal.textContent = "—";
    haptic("light");

    api("/api/download", {
      method: "POST",
      body: {
        job_id: state.jobId,
        height: height,
        audio_only: !!audioOnly
      }
    })
      .then(function (job) {
        state.busy = false;
        applyStatus(job);
        startPolling();
      })
      .catch(function (err) {
        state.busy = false;
        handleError(err);
      });
  }

  // ---------------------------------------------------------------
  // نظرسنجی وضعیت
  // ---------------------------------------------------------------
  function startPolling() {
    stopPolling();
    state.polling = setInterval(tick, POLL_MS);
  }

  function stopPolling() {
    if (state.polling) {
      clearInterval(state.polling);
      state.polling = null;
    }
  }

  function tick() {
    if (!state.jobId) { stopPolling(); return; }

    api("/api/status/" + encodeURIComponent(state.jobId))
      .then(function (job) {
        applyStatus(job);
        if (job.state === "done" || job.state === "cancelled" || job.state === "error") {
          stopPolling();
        }
      })
      .catch(function (err) {
        stopPolling();
        handleError(err);
      });
  }

  function applyStatus(job) {
    // --- پیشرفت ---
    var percent = Math.max(0, Math.min(100, job.percent || 0));
    el.progressFill.style.width = percent + "%";
    el.progressPercent.textContent = faNum(Math.round(percent)) + "٪";

    if (job.total) {
      el.progressSize.textContent = job.downloaded_text + " از " + job.total_text;
    } else if (job.downloaded) {
      el.progressSize.textContent = job.downloaded_text;
    }

    el.statSpeed.textContent = job.speed_text || "—";
    el.statDone.textContent = job.downloaded_text || "—";
    el.statTotal.textContent = job.total_text || "—";

    // --- وضعیت ---
    if (job.state === "uploading") {
      el.progressTitle.textContent = "در حال ارسال به تلگرام...";
      el.progressFill.style.width = "100%";
      el.progressPercent.textContent = "۱۰۰٪";
    } else if (job.state === "downloading") {
      el.progressTitle.textContent = "در حال دانلود...";
    }

    if (job.state === "done") {
      el.resultIco.textContent = "✅";
      el.resultTitle.textContent = "تمام شد";
      el.resultText.textContent = job.filesize_text
        ? "فایل (" + job.filesize_text + ") در چت تلگرام برایت فرستاده شد."
        : "فایل در چت تلگرام برایت فرستاده شد.";
      show(el.stepResult);
      haptic("success");
      return;
    }

    if (job.state === "cancelled") {
      el.resultIco.textContent = "🛑";
      el.resultTitle.textContent = "لغو شد";
      el.resultText.textContent = "دانلود متوقف شد و فایل نیمه‌کاره پاک شد.";
      show(el.stepResult);
      return;
    }

    if (job.state === "error") {
      showError("دانلود ناموفق", job.error_text || "خطای نامشخص.");
      return;
    }
  }

  // ---------------------------------------------------------------
  // لغو
  // ---------------------------------------------------------------
  function cancel() {
    if (!state.jobId) return;
    el.cancelBtn.disabled = true;
    el.progressTitle.textContent = "در حال لغو...";

    api("/api/cancel", { method: "POST", body: { job_id: state.jobId } })
      .then(function (job) {
        stopPolling();
        applyStatus(job);
      })
      .catch(function (err) {
        el.cancelBtn.disabled = false;
        handleError(err);
      });
  }

  // ---------------------------------------------------------------
  // بازنشانی
  // ---------------------------------------------------------------
  function reset() {
    stopPolling();
    state.jobId = null;
    state.busy = false;
    el.urlInput.value = "";
    el.clearBtn.hidden = true;
    el.cancelBtn.disabled = false;
    el.qualities.innerHTML = "";
    el.thumbImg.hidden = true;
    el.thumbImg.removeAttribute("src");
    show(el.stepLink);
    el.urlInput.focus();
  }

  // ---------------------------------------------------------------
  // رویدادها
  // ---------------------------------------------------------------
  el.probeBtn.addEventListener("click", probe);

  el.urlInput.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      probe();
    }
  });

  el.urlInput.addEventListener("input", function () {
    el.clearBtn.hidden = !el.urlInput.value;
  });

  el.clearBtn.addEventListener("click", function () {
    el.urlInput.value = "";
    el.clearBtn.hidden = true;
    el.urlInput.focus();
  });

  // چیپ پلتفرم — فقط راهنمای placeholder است، تشخیص واقعی سمت سرور انجام می‌شود
  Array.prototype.forEach.call(
    document.querySelectorAll(".chip"),
    function (chip) {
      chip.addEventListener("click", function () {
        Array.prototype.forEach.call(
          document.querySelectorAll(".chip"),
          function (other) {
            other.classList.remove("is-active");
            other.setAttribute("aria-selected", "false");
          }
        );
        chip.classList.add("is-active");
        chip.setAttribute("aria-selected", "true");

        state.platform = chip.dataset.platform;
        var placeholders = {
          youtube: "https://youtu.be/...",
          instagram: "https://instagram.com/reel/...",
          tiktok: "https://tiktok.com/@user/video/..."
        };
        el.urlInput.placeholder = placeholders[state.platform] || "https://...";
        haptic("light");
      });
    }
  );

  el.audioBtn.addEventListener("click", function () { startDownload(null, true); });
  el.backBtn.addEventListener("click", reset);
  el.cancelBtn.addEventListener("click", cancel);
  el.againBtn.addEventListener("click", reset);
  el.errorBackBtn.addEventListener("click", reset);

  // ---------------------------------------------------------------
  // شروع
  // ---------------------------------------------------------------
  function boot() {
    // نام کاربر از initData (بدون درخواست شبکه)
    if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) {
      var user = tg.initDataUnsafe.user;
      var name = user.first_name || user.username || "";
      el.greeting.textContent = name ? "سلام " + name + " 👋" : "سلام 👋";
    }

    if (!initData) {
      showError(
        "بیرون از تلگرام",
        "این صفحه فقط از داخل تلگرام کار می‌کند. ربات را باز کن و دکمه‌ی Mini App را بزن."
      );
      setConn(false);
      return;
    }

    // بررسی سلامت
    fetch("/health")
      .then(function (r) { return r.json(); })
      .then(function () { setConn(true); })
      .catch(function () { setConn(false); });

    show(el.stepLink);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
