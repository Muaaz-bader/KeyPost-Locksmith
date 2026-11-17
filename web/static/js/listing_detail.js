(() => {
  const page = document.querySelector('.listing-detail');
  if (!page) return;
  const readView = document.getElementById('listing-read-view');
  const editForm = document.getElementById('listing-edit-form');
  const feedback = document.getElementById('workflow-status');
  const crossDialog = document.getElementById('ad-cross-dialog');
  const crossMessage = document.getElementById('ad-cross-message');
  const crossButton = document.getElementById('start-cross-post');
  const deleteButton = document.getElementById('start-delete');
  const deleteOptions = document.getElementById('delete-options');
  let deleting = false;

  function message(text, error = false) {
    feedback.textContent = text || '';
    feedback.classList.toggle('is-error', error);
  }
  function closeDeleteOptions() {
    deleteOptions.hidden = true;
    deleteButton.setAttribute('aria-expanded', 'false');
  }

  document.getElementById('edit-listing').addEventListener('click', () => {
    readView.hidden = true;
    editForm.hidden = false;
    editForm.scrollIntoView({block: 'start', behavior: 'smooth'});
  });
  // ---- Photos in Edit details: toggle removal of saved photos, add new ones (previewed until saved)
  const photoBox = editForm.querySelector('.ld-edit-photos');
  const photoGrid = photoBox ? photoBox.querySelector('.ld-edit-photo-grid') : null;
  const photoInput = document.getElementById('ld-edit-photo-input');
  const removedPhotos = new Set();
  let newPhotos = [];   // [{file, url, el}]

  function updatePhotoCount() {
    if (!photoBox) return;
    const kept = photoGrid.querySelectorAll('.ld-edit-photo[data-photo-name]:not(.is-removed)').length + newPhotos.length;
    const parts = [`${kept} photo(s)`];
    if (removedPhotos.size) parts.push(`${removedPhotos.size} to remove`);
    if (newPhotos.length) parts.push(`${newPhotos.length} new`);
    document.getElementById('ld-edit-photo-count').textContent = parts.join(' · ');
  }
  function resetPhotos() {
    if (!photoBox) return;
    removedPhotos.clear();
    photoGrid.querySelectorAll('.ld-edit-photo.is-removed').forEach(el => el.classList.remove('is-removed'));
    newPhotos.forEach(photo => { URL.revokeObjectURL(photo.url); photo.el.remove(); });
    newPhotos = [];
    updatePhotoCount();
  }
  if (photoBox) {
    photoGrid.addEventListener('click', event => {
      const button = event.target.closest('[data-remove-photo]');
      if (!button) return;
      const tile = button.closest('.ld-edit-photo');
      if (tile.dataset.photoName) {   // saved photo: toggle removal (click again to keep it)
        const removed = tile.classList.toggle('is-removed');
        removed ? removedPhotos.add(tile.dataset.photoName) : removedPhotos.delete(tile.dataset.photoName);
        button.innerHTML = removed ? '&#8630;' : '&times;';
        button.setAttribute('aria-label', removed ? 'Keep this photo' : 'Remove this photo');
      } else {                        // new photo: drop it
        const photo = newPhotos.find(item => item.el === tile);
        if (photo) URL.revokeObjectURL(photo.url);
        newPhotos = newPhotos.filter(item => item.el !== tile);
        tile.remove();
      }
      updatePhotoCount();
    });
    photoInput.addEventListener('change', () => {
      const addTile = photoInput.closest('.ld-edit-photo-add');
      [...photoInput.files].forEach(file => {
        const url = URL.createObjectURL(file);
        const el = document.createElement('div');
        el.className = 'ld-edit-photo is-new';
        el.innerHTML = '<img alt=""><button type="button" class="ld-edit-photo-remove" data-remove-photo aria-label="Remove new photo">&times;</button>';
        el.querySelector('img').src = url;
        photoGrid.insertBefore(el, addTile);
        newPhotos.push({file, url, el});
      });
      photoInput.value = '';
      updatePhotoCount();
    });
    updatePhotoCount();
  }

  document.getElementById('cancel-edit-listing').addEventListener('click', () => {
    editForm.reset();
    resetPhotos();
    editForm.hidden = true;
    readView.hidden = false;
    message('');
  });
  editForm.addEventListener('submit', async event => {
    event.preventDefault();
    const button = editForm.querySelector('[type="submit"]');
    if (photoBox && !photoGrid.querySelectorAll('.ld-edit-photo:not(.is-removed)').length) {
      message('Keep at least one photo.', true);
      return;
    }
    button.disabled = true;
    try {
      const response = await appFetch(page.dataset.saveUrl, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'save_details', fields: Object.fromEntries(new FormData(editForm))})
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.message || 'Could not save the listing details.');
      if (photoBox && (removedPhotos.size || newPhotos.length)) {
        const body = new FormData();
        removedPhotos.forEach(name => body.append('remove', name));
        newPhotos.forEach(photo => body.append('photos', photo.file));
        const photoResponse = await appFetch(photoBox.dataset.photosUrl, {method: 'POST', body, timeoutMs: 120000});
        const photoResult = await photoResponse.json();
        if (!photoResponse.ok || !photoResult.ok) throw new Error('Details saved, but photos were not: ' + (photoResult.message || 'please try again.'));
      }
      window.location.reload();
    } catch (error) {
      message(error.message || 'Could not save the listing details.', true);
      button.disabled = false;
    }
  });

  document.querySelectorAll('[data-preview]').forEach(button => button.addEventListener('click', () => {
    document.getElementById('listing-hero').src = button.dataset.preview;
    document.querySelectorAll('[data-preview]').forEach(item => item.classList.toggle('is-active', item === button));
  }));

  if (crossButton && crossDialog) crossButton.addEventListener('click', () => {
    crossMessage.textContent = '';
    crossDialog.showModal();
  });
  document.querySelector('[data-cross-cancel]').addEventListener('click', () => crossDialog.close());
  document.getElementById('ad-cross-submit').addEventListener('click', async event => {
    const button = event.currentTarget;
    const targets = [...crossDialog.querySelectorAll('input[type="checkbox"]:checked')].map(input => input.value);
    if (!targets.length || targets.length >= 20) {
      crossMessage.textContent = 'Select 1 to 19 eligible accounts.';
      return;
    }
    button.disabled = true;
    crossMessage.textContent = 'Queueing service ad...';
    try {
      const response = await appFetch(page.dataset.crossUrl, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          account: page.dataset.account,
          listing_id: page.dataset.listingId,
          target_accounts: targets,
          fields: JSON.parse(page.dataset.adFields || '{}'),
          image_order: JSON.parse(page.dataset.imageOrder || '[]'),
          post_mode: 'now',
          backup_post: crossButton.dataset.adBackup === 'true',
          posting_requested_at_ms: Date.now()
        })
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.message || 'Could not queue the service ad.');
      crossDialog.close();
      message(result.message || 'Service ad queued. Check Posting Results.');
    } catch (error) {
      crossMessage.textContent = error.message || 'Could not reach the app. Try again.';
    } finally {
      button.disabled = false;
    }
  });

  deleteButton.addEventListener('click', () => {
    if (deleting) return;
    deleteOptions.hidden = !deleteOptions.hidden;
    deleteButton.setAttribute('aria-expanded', String(!deleteOptions.hidden));
  });
  document.getElementById('cancel-delete').addEventListener('click', closeDeleteOptions);
  deleteOptions.addEventListener('click', async event => {
    const choice = event.target.closest('[data-delete-target]');
    if (!choice || deleting) return;
    deleting = true;
    closeDeleteOptions();
    deleteButton.disabled = true;
    message('Starting deletion...');
    try {
      const response = await appFetch(page.dataset.deleteUrl, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ids: [page.dataset.listingId], target: choice.dataset.deleteTarget, account: page.dataset.account})
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.message || 'Deletion could not start.');
      if (result.completed) { window.location.assign(page.dataset.listingsUrl); return; }
      message('Deletion started. This page will update when it finishes.');
    } catch (error) {
      message(error.message || 'Could not start deletion.', true);
      deleteButton.disabled = false;
      deleting = false;
    }
  });
})();
