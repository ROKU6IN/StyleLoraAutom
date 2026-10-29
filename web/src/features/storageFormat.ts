export function storageSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  const unit = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), 3);
  return `${(bytes / 1024 ** unit).toFixed(2)} ${['B', 'KiB', 'MiB', 'GiB'][unit]}`;
}
