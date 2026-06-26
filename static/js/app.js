// Alpine.js component functions — globally available

function uploadForm(maxFiles) {
  maxFiles = maxFiles || 5;
  return {
    files: [],
    previews: [],
    isDragging: false,

    handleFiles(fileList) {
      const arr = Array.from(fileList).slice(0, maxFiles);
      this.files = arr;
      this.previews = [];
      arr.forEach(f => {
        const r = new FileReader();
        r.onload = e => this.previews.push({ url: e.target.result, name: f.name, size: f.size });
        r.readAsDataURL(f);
      });
    },

    handleDrop(e) {
      this.isDragging = false;
      const allowed = ['image/jpeg', 'image/png', 'image/gif', 'image/webp'];
      const dropped = Array.from(e.dataTransfer.files)
        .filter(f => allowed.includes(f.type))
        .slice(0, maxFiles);
      if (!dropped.length) return;
      const dt = new DataTransfer();
      dropped.forEach(f => dt.items.add(f));
      this.$refs.fileInput.files = dt.files;
      this.handleFiles(dropped);
    },

    removeFile(i) {
      this.files.splice(i, 1);
      this.previews.splice(i, 1);
      const dt = new DataTransfer();
      this.files.forEach(f => dt.items.add(f));
      this.$refs.fileInput.files = dt.files;
    },

    formatSize(b) {
      if (b < 1024) return b + ' B';
      if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
      return (b / 1048576).toFixed(1) + ' MB';
    }
  };
}

function copyLink() {
  return {
    copied: false,
    doCopy(text) {
      navigator.clipboard.writeText(text).then(() => {
        this.copied = true;
        setTimeout(() => this.copied = false, 2500);
      }).catch(() => {
        // Fallback for older browsers
        const el = document.createElement('textarea');
        el.value = text;
        document.body.appendChild(el);
        el.select();
        document.execCommand('copy');
        document.body.removeChild(el);
        this.copied = true;
        setTimeout(() => this.copied = false, 2500);
      });
    }
  };
}

function countdown(expiresAtStr) {
  return {
    expiresAt: new Date(expiresAtStr + 'Z'),
    timeLeft: '',
    minutesLeft: 60,
    _timer: null,

    init() {
      this.tick();
      this._timer = setInterval(() => this.tick(), 1000);
    },

    tick() {
      const diff = this.expiresAt - Date.now();
      if (diff <= 0) {
        this.timeLeft = 'Expired';
        this.minutesLeft = 0;
        clearInterval(this._timer);
        return;
      }
      const m = Math.floor(diff / 60000);
      const s = Math.floor((diff % 60000) / 1000);
      this.minutesLeft = m;
      this.timeLeft = m + 'm ' + String(s).padStart(2, '0') + 's left';
    }
  };
}
