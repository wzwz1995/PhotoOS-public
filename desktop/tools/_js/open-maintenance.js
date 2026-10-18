(() => {
  const tab = document.querySelector('[data-view="maintenance"]');
  if (!tab) return 'no maintenance tab';
  tab.click();
  return 'clicked';
})()
