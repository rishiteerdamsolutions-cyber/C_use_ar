/**
 * Portal: mobile nav + support contact (reason → prefilled WhatsApp / email).
 */
(function () {
  var WA_NUMBER = "919505009699";
  var EMAIL = "aideveloperindia@gmail.com";

  var REASONS = [
    {
      id: "sales",
      label: "Sales or product enquiry",
      waText:
        "Hello — I'm reaching out about cusear™ (sales / product enquiry).\n\nPlease share details:",
      emailSubject: "cusear™ — Sales / product enquiry",
      emailBody:
        "Hello,\n\nI'm interested in learning more about cusear™.\n\n",
    },
    {
      id: "billing",
      label: "Billing, invoice & refunds",
      waText:
        "Hello — I need help with billing, an invoice, or a refund for cusear™.\n\nOrder / account details:",
      emailSubject: "cusear™ — Billing / invoice / refund",
      emailBody:
        "Hello,\n\nI need assistance with billing, an invoice, or a refund.\n\nDetails:\n\n",
    },
    {
      id: "technical",
      label: "Technical support & bugs",
      waText:
        "Hello — I need technical support for cusear™ (bug or how-to).\n\nWhat I tried:",
      emailSubject: "cusear™ — Technical support",
      emailBody:
        "Hello,\n\nI need technical support with cusear™.\n\nIssue / steps to reproduce:\n\n",
    },
    {
      id: "account",
      label: "Account access & onboarding",
      waText:
        "Hello — I need help with my cusear™ account or getting started.\n\nEmail I signed up with:",
      emailSubject: "cusear™ — Account / onboarding",
      emailBody:
        "Hello,\n\nI need help with account access or onboarding.\n\n",
    },
    {
      id: "partnership",
      label: "Partnership or enterprise",
      waText:
        "Hello — I'd like to discuss a partnership or enterprise use of cusear™.\n\nCompany / use case:",
      emailSubject: "cusear™ — Partnership / enterprise",
      emailBody:
        "Hello,\n\nI'd like to discuss partnership or enterprise options for cusear™.\n\n",
    },
    {
      id: "other",
      label: "Other",
      waText: "Hello — I'm contacting cusear™ support.\n\nMessage:",
      emailSubject: "cusear™ — Support",
      emailBody: "Hello,\n\n",
    },
  ];

  function $(sel, root) {
    return (root || document).querySelector(sel);
  }
  function $all(sel, root) {
    return [].slice.call((root || document).querySelectorAll(sel));
  }

  function closeAllSupportPops() {
    $all(".support-pop").forEach(function (p) {
      p.classList.remove("is-open");
    });
    $all(".support-icon-btn").forEach(function (b) {
      b.setAttribute("aria-expanded", "false");
    });
  }

  function buildReasonButtons(panel, channel) {
    if (panel.getAttribute("data-built") === "1") return;
    panel.setAttribute("data-built", "1");
    REASONS.forEach(function (r) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "support-reason";
      btn.textContent = r.label;
      btn.addEventListener("click", function () {
        if (channel === "whatsapp") {
          var url =
            "https://wa.me/" +
            WA_NUMBER +
            "?text=" +
            encodeURIComponent(r.waText);
          window.open(url, "_blank", "noopener,noreferrer");
        } else {
          var mail =
            "mailto:" +
            EMAIL +
            "?subject=" +
            encodeURIComponent(r.emailSubject) +
            "&body=" +
            encodeURIComponent(r.emailBody);
          window.location.href = mail;
        }
        closeAllSupportPops();
      });
      panel.appendChild(btn);
    });
  }

  function initSupport() {
    $all(".support-anchor").forEach(function (anchor) {
      var btn = anchor.querySelector(".support-icon-btn");
      var pop = anchor.querySelector(".support-pop");
      if (!btn || !pop) return;
      var channel = btn.getAttribute("data-channel") || "whatsapp";
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        var open = !pop.classList.contains("is-open");
        closeAllSupportPops();
        if (open) {
          buildReasonButtons(pop, channel);
          pop.classList.add("is-open");
          btn.setAttribute("aria-expanded", "true");
        }
      });
    });

    document.addEventListener("click", function () {
      closeAllSupportPops();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeAllSupportPops();
    });
    $all(".support-pop").forEach(function (p) {
      p.addEventListener("click", function (e) {
        e.stopPropagation();
      });
    });
  }

  function initNav() {
    var nav = document.getElementById("siteNav");
    if (!nav) return;
    var toggle = document.getElementById("navToggle");
    function setOpen(open) {
      nav.classList.toggle("nav--open", open);
      if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
    }
    if (toggle) {
      toggle.addEventListener("click", function (e) {
        e.preventDefault();
        setOpen(!nav.classList.contains("nav--open"));
      });
    }
    nav.querySelectorAll(".nav-links a").forEach(function (a) {
      a.addEventListener("click", function () {
        setOpen(false);
      });
    });
    var logo = nav.querySelector(".nav-logo");
    if (logo) {
      logo.addEventListener("click", function () {
        setOpen(false);
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      initNav();
      initSupport();
    });
  } else {
    initNav();
    initSupport();
  }
})();
