document$.subscribe(() => {
  const link = document.querySelector(
    '.md-sidebar--primary a[href$="using/"]',
  );
  const section = link?.closest('.md-nav__item--nested');
  const toggle = section?.querySelector(':scope > .md-nav__toggle');
  if (toggle) {
    toggle.checked = true;
  }
});
