import { $ } from './util.js';
import { AppState, token } from './state.js';



    function openReader(game, index, customUrl) {
      const doc = game.documents[index];
      if (!doc) return;
      AppState.readerPage = 1;
      AppState.readerUrl = customUrl || `/api/document?id=${game.id}&index=${index}&token=${encodeURIComponent(token)}`;
      $('readerTitle').textContent = doc.name;
      $('readerViewport').classList.remove('spread');
      $('readerFrame').style.filter = '';
      setReaderPage(1);
      $('readerDialog').showModal();
    }
    function setReaderPage(page) {
      AppState.readerPage = Math.max(1, page);
      const suffix = AppState.readerUrl.toLowerCase().includes('.pdf') || AppState.readerUrl.includes('/api/document') ? `#page=${AppState.readerPage}` : '';
      $('readerFrame').src = `${AppState.readerUrl}${suffix}`;
      $('readerPageLabel').textContent = `Page ${AppState.readerPage}`;
    }
    document.querySelectorAll('[data-reader-layout]').forEach(button => button.onclick = () => {
      $('readerViewport').classList.toggle('spread', button.dataset.readerLayout === 'spread');
    });
    document.querySelectorAll('[data-reader-theme]').forEach(button => button.onclick = () => {
      $('readerFrame').style.filter = button.dataset.readerTheme === 'dark' ? 'invert(1) hue-rotate(180deg)' : '';
    });
    $('readerPrev').onclick = () => setReaderPage(AppState.readerPage - 1);
    $('readerNext').onclick = () => setReaderPage(AppState.readerPage + 1);

export { openReader, setReaderPage };
