document.addEventListener("DOMContentLoaded", () => {
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
