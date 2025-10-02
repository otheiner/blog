function updateNav() {
  const nav = document.querySelector('.site-nav');
  const menu = document.querySelector('.nav-menu');
  const toggle = document.querySelector('.nav-toggle');

  // Measure total width of menu items
  const menuWidth = Array.from(menu.children).reduce(
    (total, item) => total + item.offsetWidth,
    0
  );

  // Available width for nav
  const wrapperWidth = nav.clientWidth - 40; // 40px buffer for spacing/logo

  if (menuWidth > wrapperWidth) {
    // Switch to mobile dropdown
    menu.classList.add('mobile');
    toggle.style.display = 'block';
  } else {
    // Keep inline menu
    menu.classList.remove('mobile', 'show');
    toggle.style.display = 'none';
    document.getElementById('nav-trigger').checked = false;
  }
}

// Toggle dropdown when checkbox is clicked
document.querySelector('.nav-trigger').addEventListener('change', function() {
  const menu = document.querySelector('.nav-menu');
  menu.classList.toggle('show', this.checked);
});

window.addEventListener('resize', updateNav);
window.addEventListener('load', updateNav);
