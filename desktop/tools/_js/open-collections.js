(() => {
  const tab = document.querySelector('[data-view="collections"]');
  if (!tab) return 'no collections tab';
  tab.click();
  return 'clicked';
})()
