/* g2d-shim.c - software i.MX G2D for the XDJ-RX3 player under WSL, linked into fbshim.so.
   The player draws the big waveform display with Freescale's libg2d (Vivante GPU blits into the frame buffer
   through /dev/galcore). No GPU here, so the real g2d_open fails and the centre panel stays blank. These exports
   shadow libg2d.so.0.8 (LD_PRELOAD wins) and do the blits on the CPU.
   Physical addresses: DirectFB's fbdev pool hands out smem_start(=0) + offset for surfaces inside the frame
   buffer, so paddr < fb size means "inside the fb mapping"; g2d_alloc buffers use paddr == vaddr.
   Built -nostdlib: raw declarations only. */
extern void *malloc(unsigned);
extern void free(void *);
extern void *dlsym(void *, const char *);

static int fb_fd = -1;
static unsigned char *fb_base;
static unsigned long fb_len;

static int str_eq(const char *a, const char *b)
{
   while (*a && *a == *b) { a++; b++; }
   return *a == *b;
}

int open(const char *p, int f, ...)
{
   static int (*real)(const char *, int, int);
   if (!real) real = dlsym((void *)-1, "open");
   __builtin_va_list ap; __builtin_va_start(ap, f); int m = __builtin_va_arg(ap, int); __builtin_va_end(ap);
   int fd = real(p, f, m);
   if (fd >= 0 && p && str_eq(p, "/dev/fb0")) fb_fd = fd;
   return fd;
}

int open64(const char *p, int f, ...)
{
   static int (*real)(const char *, int, int);
   if (!real) real = dlsym((void *)-1, "open64");
   __builtin_va_list ap; __builtin_va_start(ap, f); int m = __builtin_va_arg(ap, int); __builtin_va_end(ap);
   int fd = real(p, f, m);
   if (fd >= 0 && p && str_eq(p, "/dev/fb0")) fb_fd = fd;
   return fd;
}

void *mmap(void *a, unsigned long len, int prot, int flags, int fd, long off)
{
   static void *(*real)(void *, unsigned long, int, int, int, long);
   if (!real) real = dlsym((void *)-1, "mmap");
   void *r = real(a, len, prot, flags, fd, off);
   if (r != (void *)-1 && fd >= 0 && fd == fb_fd && off == 0) { fb_base = r; fb_len = len; }
   return r;
}

void *mmap64(void *a, unsigned long len, int prot, int flags, int fd, long long off)
{
   static void *(*real)(void *, unsigned long, int, int, int, long long);
   if (!real) real = dlsym((void *)-1, "mmap64");
   void *r = real(a, len, prot, flags, fd, off);
   if (r != (void *)-1 && fd >= 0 && fd == fb_fd && off == 0) { fb_base = r; fb_len = len; }
   return r;
}

struct g2d_surface { int format; int planes[3]; int left, top, right, bottom; int stride; int width, height; int blendfunc; int global_alpha; int clrcolor; int rot; };
struct g2d_buf { void *buf_handle; void *buf_vaddr; int buf_paddr; int buf_size; };

static int g2d_handle_dummy;
int g2d_open(void **h) { if (h) *h = &g2d_handle_dummy; return 0; }
int g2d_close(void *h) { (void)h; return 0; }
int g2d_make_current(void *h, int type) { (void)h; (void)type; return 0; }
int g2d_finish(void *h) { (void)h; return 0; }
int g2d_flush(void *h) { (void)h; return 0; }
int g2d_cache_op(struct g2d_buf *b, int op) { (void)b; (void)op; return 0; }
int g2d_query_cap(void *h, int cap, int *enable) { (void)h; (void)cap; if (enable) *enable = 0; return 0; }
int g2d_enable(void *h, int cap) { (void)h; (void)cap; return 0; }
int g2d_disable(void *h, int cap) { (void)h; (void)cap; return 0; }

struct g2d_buf *g2d_alloc(int size, int cacheable)
{
   (void)cacheable;
   struct g2d_buf *b = malloc(sizeof *b);
   if (!b) return 0;
   unsigned char *m = malloc((unsigned)size + 64);
   if (!m) { free(b); return 0; }
   unsigned long al = ((unsigned long)m + 63) & ~63ul;
   b->buf_handle = m; b->buf_vaddr = (void *)al; b->buf_paddr = (int)al; b->buf_size = size;
   return b;
}

int g2d_free(struct g2d_buf *b) { if (!b) return -1; free(b->buf_handle); free(b); return 0; }

int g2d_copy(void *h, struct g2d_buf *d, struct g2d_buf *s, int size)
{
   (void)h;
   if (!d || !s) return -1;
   unsigned char *a = d->buf_vaddr, *b = s->buf_vaddr;
   for (int i = 0; i < size; i++) a[i] = b[i];
   return 0;
}

static unsigned char *g2d_ptr(int paddr)
{
   unsigned long p = (unsigned long)(unsigned)paddr;
   if (fb_base && p < fb_len) return fb_base + p;
   return (unsigned char *)p;
}

static int g2d_bpp(int f) { return f == 0 || f == 5 ? 2 : f == 10 ? 3 : 4; }

/* read a pixel as A,R,G,B (g2d format names give the byte order in memory) */
static void g2d_rd(int f, const unsigned char *p, unsigned *a, unsigned *r, unsigned *g, unsigned *b)
{
   switch (f) {
      case 0: { unsigned v = p[0] | p[1] << 8; *r = (v >> 11) << 3; *g = ((v >> 5) & 63) << 2; *b = (v & 31) << 3; *a = 255; return; }   /* RGB565 */
      case 5: { unsigned v = p[0] | p[1] << 8; *b = (v >> 11) << 3; *g = ((v >> 5) & 63) << 2; *r = (v & 31) << 3; *a = 255; return; }   /* BGR565 */
      case 1: *r = p[0]; *g = p[1]; *b = p[2]; *a = p[3]; return;   /* RGBA8888 */
      case 2: *r = p[0]; *g = p[1]; *b = p[2]; *a = 255; return;    /* RGBX8888 */
      case 3: *b = p[0]; *g = p[1]; *r = p[2]; *a = p[3]; return;   /* BGRA8888 */
      case 4: *b = p[0]; *g = p[1]; *r = p[2]; *a = 255; return;    /* BGRX8888 */
      case 6: *a = p[0]; *r = p[1]; *g = p[2]; *b = p[3]; return;   /* ARGB8888 */
      case 7: *a = p[0]; *b = p[1]; *g = p[2]; *r = p[3]; return;   /* ABGR8888 */
      case 8: *r = p[1]; *g = p[2]; *b = p[3]; *a = 255; return;    /* XRGB8888 */
      case 9: *b = p[1]; *g = p[2]; *r = p[3]; *a = 255; return;    /* XBGR8888 */
      case 10: *r = p[0]; *g = p[1]; *b = p[2]; *a = 255; return;   /* RGB888 */
      default: *b = p[0]; *g = p[1]; *r = p[2]; *a = p[3]; return;
   }
}

static void g2d_wr(int f, unsigned char *p, unsigned a, unsigned r, unsigned g, unsigned b)
{
   switch (f) {
      case 0: { unsigned v = (r >> 3) << 11 | (g >> 2) << 5 | b >> 3; p[0] = v; p[1] = v >> 8; return; }
      case 5: { unsigned v = (b >> 3) << 11 | (g >> 2) << 5 | r >> 3; p[0] = v; p[1] = v >> 8; return; }
      case 1: p[0] = r; p[1] = g; p[2] = b; p[3] = a; return;
      case 2: p[0] = r; p[1] = g; p[2] = b; p[3] = 255; return;
      case 3: p[0] = b; p[1] = g; p[2] = r; p[3] = a; return;
      case 4: p[0] = b; p[1] = g; p[2] = r; p[3] = 255; return;
      case 6: p[0] = a; p[1] = r; p[2] = g; p[3] = b; return;
      case 7: p[0] = a; p[1] = b; p[2] = g; p[3] = r; return;
      case 8: p[0] = 255; p[1] = r; p[2] = g; p[3] = b; return;
      case 9: p[0] = 255; p[1] = b; p[2] = g; p[3] = r; return;
      case 10: p[0] = r; p[1] = g; p[2] = b; return;
      default: p[0] = b; p[1] = g; p[2] = r; p[3] = a; return;
   }
}

/* blend factors: G2D_ZERO 0, ONE 1, SRC_ALPHA 2, ONE_MINUS_SRC_ALPHA 3, DST_ALPHA 4, ONE_MINUS_DST_ALPHA 5 */
static unsigned g2d_factor(int func, unsigned sa, unsigned da)
{
   switch (func & 0xf) { case 1: return 255; case 2: return sa; case 3: return 255 - sa; case 4: return da; case 5: return 255 - da; default: return 0; }
}

int g2d_blit(void *h, struct g2d_surface *s, struct g2d_surface *d)
{
   (void)h;
   if (!s || !d) return -1;
   unsigned char *sp = g2d_ptr(s->planes[0]), *dp = g2d_ptr(d->planes[0]);
   if (!sp || !dp) return -1;
   int sw = s->right - s->left, sh = s->bottom - s->top, dw = d->right - d->left, dh = d->bottom - d->top;
   if (sw <= 0 || sh <= 0 || dw <= 0 || dh <= 0) return 0;
   int sbpp = g2d_bpp(s->format), dbpp = g2d_bpp(d->format);
   int sstride = (s->stride > 0 ? s->stride : s->width) * sbpp, dstride = (d->stride > 0 ? d->stride : d->width) * dbpp;
   int blend = (s->blendfunc | d->blendfunc) != 0 && !(s->blendfunc == 1 && d->blendfunc == 0);
   unsigned galpha = (s->global_alpha > 0 && s->global_alpha < 255) ? (unsigned)s->global_alpha : 255;
   int rot = s->rot ? s->rot : d->rot;
   for (int y = 0; y < dh; y++) {
      int dy = d->top + y;
      if (dy < 0 || dy >= d->height) continue;
      for (int x = 0; x < dw; x++) {
         int dx = d->left + x;
         if (dx < 0 || dx >= d->width) continue;
         int u, v;
         switch (rot) {
            case 1: u = (int)((long)y * sw / dh); v = (int)((long)(dw - 1 - x) * sh / dw); break;             /* 90 */
            case 2: u = (int)((long)(dw - 1 - x) * sw / dw); v = (int)((long)(dh - 1 - y) * sh / dh); break;   /* 180 */
            case 3: u = (int)((long)(dh - 1 - y) * sw / dh); v = (int)((long)x * sh / dw); break;             /* 270 */
            case 4: u = (int)((long)(dw - 1 - x) * sw / dw); v = (int)((long)y * sh / dh); break;             /* flip h */
            case 5: u = (int)((long)x * sw / dw); v = (int)((long)(dh - 1 - y) * sh / dh); break;             /* flip v */
            default: u = (int)((long)x * sw / dw); v = (int)((long)y * sh / dh); break;
         }
         int sx = s->left + u, sy = s->top + v;
         if (sx < 0 || sy < 0 || sx >= s->width || sy >= s->height) continue;
         const unsigned char *ps = sp + (long)sy * sstride + (long)sx * sbpp;
         unsigned char *pd = dp + (long)dy * dstride + (long)dx * dbpp;
         unsigned sa, sr, sg, sb;
         g2d_rd(s->format, ps, &sa, &sr, &sg, &sb);
         sa = sa * galpha / 255;
         if (!blend) { g2d_wr(d->format, pd, sa, sr, sg, sb); continue; }
         unsigned da, dr, dg, db;
         g2d_rd(d->format, pd, &da, &dr, &dg, &db);
         unsigned fs = g2d_factor(s->blendfunc, sa, da), fd = g2d_factor(d->blendfunc, sa, da);
         unsigned r = (sr * fs + dr * fd) / 255, g = (sg * fs + dg * fd) / 255, b = (sb * fs + db * fd) / 255, a = (sa * fs + da * fd) / 255;
         g2d_wr(d->format, pd, a > 255 ? 255 : a, r > 255 ? 255 : r, g > 255 ? 255 : g, b > 255 ? 255 : b);
      }
   }
   return 0;
}

int g2d_clear(void *h, struct g2d_surface *d)
{
   (void)h;
   if (!d) return -1;
   unsigned char *dp = g2d_ptr(d->planes[0]);
   if (!dp) return -1;
   int bpp = g2d_bpp(d->format), stride = (d->stride > 0 ? d->stride : d->width) * bpp;
   unsigned c = (unsigned)d->clrcolor;
   for (int y = d->top; y < d->bottom && y < d->height; y++)
      for (int x = d->left; x < d->right && x < d->width; x++)
         if (x >= 0 && y >= 0) g2d_wr(d->format, dp + (long)y * stride + (long)x * bpp, c >> 24, (c >> 16) & 255, (c >> 8) & 255, c & 255);
   return 0;
}
