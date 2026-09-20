/* wsl-shim.c - extra LD_PRELOAD interposers for running the XDJ-RX3 player under proot + qemu-arm on WSL,
   linked into fbshim.so next to the Pi project's fbshim.c / control-shim.c. Built -nostdlib like the rest of
   the shim: raw declarations only.
   - Real-time scheduling: the firmware's acre_tsk needs sched_setscheduler/pthread_setschedparam to succeed
     (otherwise CmnFunc_Error sleeps forever). Unprivileged qemu-user cannot grant RT, so report success.
   - Audio capture + pacing: there is no sound card. The outputs go to ALSA "null" devices, which do not pace
     writes in this alsa-lib, so wsl_capture() (called from snd_pcm_writei in the patched fbshim) copies every
     buffer to /tmp/rx3-master.raw or /tmp/rx3-cue.raw (S16_LE 44.1 kHz stereo) and sleeps so the audio thread
     runs at real time. Files are truncated at 32 MB; the bridge notices the shrink and resumes tailing. */
int sched_setscheduler(int pid, int policy, const void *param) { (void)pid; (void)policy; (void)param; return 0; }
int sched_setparam(int pid, const void *param) { (void)pid; (void)param; return 0; }
int pthread_setschedparam(unsigned long th, int policy, const void *param) { (void)th; (void)policy; (void)param; return 0; }
int pthread_setschedprio(unsigned long th, int prio) { (void)th; (void)prio; return 0; }
int pthread_attr_setschedpolicy(void *attr, int policy) { (void)attr; (void)policy; return 0; }
int pthread_attr_setinheritsched(void *attr, int inherit) { (void)attr; (void)inherit; return 0; }
int mlockall(int flags) { (void)flags; return 0; }
int setpriority(int which, unsigned who, int prio) { (void)which; (void)who; (void)prio; return 0; }

extern int open(const char *, int, ...);
extern int close(int);
extern int write(int, const void *, unsigned);
extern int usleep(unsigned);
typedef struct { long s, ns; } wsl_ts;
extern int clock_gettime(int, wsl_ts *);
static long long wsl_now_us(void) { wsl_ts t; clock_gettime(1, &t); return (long long)t.s * 1000000 + t.ns / 1000; }
static int cap_fd[2] = { -1, -1 };
static unsigned long cap_bytes[2];
static long long cap_due;
#define CAP_MAX (32u * 1024u * 1024u)

void wsl_capture(const char *target, const void *buf, unsigned long frames, unsigned channels, int format, unsigned rate)
{
   int which = target && target[3] == 'o' ? 0 : 1;   /* "rx3out" = master, "rx3cue" = headphones */
   if (cap_fd[which] < 0 || cap_bytes[which] > CAP_MAX) {
      if (cap_fd[which] >= 0) close(cap_fd[which]);
      cap_fd[which] = open(which == 0 ? "/tmp/rx3-master.raw" : "/tmp/rx3-cue.raw", 01 | 0100 | 01000, 0644);   /* O_WRONLY|O_CREAT|O_TRUNC */
      cap_bytes[which] = 0;
   }
   if (channels < 1 || channels > 8) channels = 2;
   unsigned long samples = frames * channels;
   if (cap_fd[which] >= 0 && samples) {
      /* convert to S16_LE stereo: format 2 = S16_LE, 6 = S24_LE (24 bits in a 32-bit word), 10 = S32_LE */
      static short out[4096 * 2];
      unsigned long done = 0;
      while (done < samples) {
         unsigned long n = samples - done; if (n > sizeof out / sizeof out[0]) n = sizeof out / sizeof out[0];
         unsigned long i;
         if (format == 6) { const int *s = (const int *)buf + done; for (i = 0; i < n; i++) out[i] = (short)(((unsigned)s[i] << 8) >> 16); }
         else if (format == 10) { const int *s = (const int *)buf + done; for (i = 0; i < n; i++) out[i] = (short)(s[i] >> 16); }
         else { const short *s = (const short *)buf + done; for (i = 0; i < n; i++) out[i] = s[i]; }
         if (channels != 2) {   /* fold to stereo: first two channels */
            unsigned long f, k = 0; for (f = 0; f < n / channels; f++) { out[k++] = out[f * channels]; out[k++] = out[f * channels + (channels > 1 ? 1 : 0)]; } n = k;
         }
         const char *p = (const char *)out; unsigned long left = n * 2;
         while (left) { int w = write(cap_fd[which], p, (unsigned)left); if (w <= 0) break; p += w; left -= (unsigned long)w; }
         cap_bytes[which] += n * 2;
         done += (channels != 2) ? (n / 2) * channels : n;
         if (channels != 2) break;   /* one pass is enough for the odd layouts */
      }
   }
   if (which != 0) return;   /* the master output sets the pace; the cue mix rides along */
   if (rate < 8000) rate = 44100;
   long long t = wsl_now_us();
   if (cap_due < t - 200000 || cap_due > t + 1000000) cap_due = t;
   cap_due += (long long)(frames * 10000u / (rate / 100u));
   if (cap_due > t) usleep((unsigned)(cap_due - t));
}

/* Exact-rate negotiation for the null outputs: set the rate to a single value (44100 unless the firmware asks
   for something else) so the params carry one rate and snd_pcm_hw_params_get_rate() succeeds. */
extern void *dlvsym(void *, const char *, const char *);
int wsl_set_rate(void *pcm, void *params, unsigned *rate, int *dir, void *real_near)
{
   static int (*set_rate)(void *, void *, unsigned, int);
   if (!set_rate) set_rate = dlvsym((void *)-1, "snd_pcm_hw_params_set_rate", "ALSA_0.9.0rc4");
   unsigned want = rate && *rate >= 8000 ? *rate : 44100;
   if (set_rate && set_rate(pcm, params, want, 0) >= 0) { if (rate) *rate = want; if (dir) *dir = 0; return 0; }
   int (*near)(void *, void *, unsigned *, int *) = real_near;
   return near(pcm, params, rate, dir);
}

/* Fixed codec personality for the null outputs: exactly 44100 Hz, S16_LE, stereo, interleaved. */
void wsl_constrain(void *pcm, void *params)
{
   static int (*set_rate)(void *, void *, unsigned, int), (*set_channels)(void *, void *, unsigned), (*set_format)(void *, void *, int), (*set_access)(void *, void *, int);
   if (!set_rate) {
      set_rate = dlvsym((void *)-1, "snd_pcm_hw_params_set_rate", "ALSA_0.9.0rc4");
      set_channels = dlvsym((void *)-1, "snd_pcm_hw_params_set_channels", "ALSA_0.9.0rc4");
      set_format = dlvsym((void *)-1, "snd_pcm_hw_params_set_format", "ALSA_0.9.0rc4");
      set_access = dlvsym((void *)-1, "snd_pcm_hw_params_set_access", "ALSA_0.9.0rc4");
   }
   if (set_access) set_access(pcm, params, 3);        /* SND_PCM_ACCESS_RW_INTERLEAVED */
   if (set_channels) set_channels(pcm, params, 2);
   if (set_rate) set_rate(pcm, params, 44100, 0);
}
