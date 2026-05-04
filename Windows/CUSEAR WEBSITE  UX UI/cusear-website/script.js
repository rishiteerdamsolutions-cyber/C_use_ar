document.addEventListener("DOMContentLoaded", () => {
  /** YouTube demo embeds: muted autoplay + loop when scrolled into view (no click). Paste 11-char id in data-youtube-id on each iframe in index.html. */
  function wireYoutubeDemoIframes() {
    const frames = document.querySelectorAll("iframe.youtube-demo[data-youtube-id]");
    if (!frames.length) return;

    function validYoutubeId(raw) {
      const s = (raw || "").trim();
      return /^[\w-]{11}$/.test(s);
    }

    function activate(frame) {
      if (frame.dataset.youtubeActivated === "1") return;
      const id = (frame.getAttribute("data-youtube-id") || "").trim();
      if (!validYoutubeId(id)) return;
      frame.dataset.youtubeActivated = "1";
      const u = new URL(`https://www.youtube-nocookie.com/embed/${encodeURIComponent(id)}`);
      u.searchParams.set("autoplay", "1");
      u.searchParams.set("mute", "1");
      u.searchParams.set("loop", "1");
      u.searchParams.set("playlist", id);
      u.searchParams.set("playsinline", "1");
      u.searchParams.set("modestbranding", "1");
      u.searchParams.set("rel", "0");
      u.searchParams.set("controls", "1");
      frame.src = u.toString();
    }

    const io = new IntersectionObserver(
      (entries, obs) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          activate(entry.target);
          obs.unobserve(entry.target);
        });
      },
      { root: null, rootMargin: "120px 0px", threshold: 0.12 }
    );

    frames.forEach((f) => {
      if (validYoutubeId(f.getAttribute("data-youtube-id"))) io.observe(f);
    });
  }

  wireYoutubeDemoIframes();

  // Typewriter Utility
  function typeWriter(element, text, speed=30) {
    element.innerHTML = '';
    let i = 0;
    function type() {
      if (i < text.length) {
        element.innerHTML += text.charAt(i);
        i++;
        setTimeout(type, speed);
      }
    }
    type();
  }
  // Intersection Observer for scroll animations
  const observerOptions = {
    root: null,
    rootMargin: '0px',
    threshold: 0.15
  };

  const observer = new IntersectionObserver((entries, observer) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('visible');
        observer.unobserve(entry.target); // Only animate once
      }
    });
  }, observerOptions);

  const elementsToAnimate = document.querySelectorAll('.fade-in');
  elementsToAnimate.forEach(el => observer.observe(el));

  // Toggle Full Pricing
  const toggleBtn = document.getElementById('toggle-pricing-btn');
  const fullPricing = document.getElementById('full-pricing-container');
  
  if (toggleBtn && fullPricing) {
    toggleBtn.addEventListener('click', () => {
      fullPricing.classList.toggle('hidden');
      if (!fullPricing.classList.contains('hidden')) {
        toggleBtn.textContent = 'Hide full pricing';
        fullPricing.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        
        // Trigger typewriter
        const typewriters = document.querySelectorAll('.typewriter-text');
        typewriters.forEach(el => {
          const text = el.getAttribute('data-text');
          typeWriter(el, text, 30);
        });
      } else {
        toggleBtn.textContent = 'Open full pricing';
        // Clear typewriters so they animate fresh next time
        document.querySelectorAll('.typewriter-text').forEach(el => el.innerHTML = '');
      }
    });
  }


});
