/*
 * memfs.c — LD_PRELOAD RAM filesystem shim for CADO-NFS
 *
 * Intercepts libc file-system calls and redirects all operations whose
 * path begins with MEMFS_ROOT (default: /cado-work) to anonymous in-RAM
 * files created via memfd_create(2). This lets CADO-NFS store tens of GB
 * of working files in the host's free RAM even though the container's /tmp
 * is limited to 1 GB.
 *
 * Intercepted calls (path-based only; fd-based I/O is passed through):
 *   open / open64 / openat / openat64 / creat / creat64
 *   fopen / fopen64 / freopen / freopen64
 *   stat / stat64 / lstat / lstat64 / fstatat / __xstat / __xstat64
 *   access / faccessat
 *   rename / renameat
 *   unlink / unlinkat / remove
 *   mkdir / mkdirat / rmdir
 *   opendir / fdopendir / readdir / readdir64 / rewinddir / closedir
 *
 * Build:
 *   gcc -O2 -fPIC -shared -o memfs.so memfs.c -ldl -lpthread
 *
 * Usage:
 *   MEMFS_ROOT=/cado-work LD_PRELOAD=/usr/local/lib/memfs.so cado-nfs.py ...
 */

#define _GNU_SOURCE
#include <stdint.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <dirent.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <stdarg.h>
#include <pthread.h>
#include <time.h>
#include <limits.h>

/* ------------------------------------------------------------------ */
/* Configuration                                                        */
/* ------------------------------------------------------------------ */

#define VFS_MAX_ENTRIES   65536   /* max files + dirs tracked          */
#define VFS_MAX_NAME      256     /* max filename component length      */
#define VFS_MAX_PATH      4096    /* max full-path length               */
#define VFS_MAX_OPEN_DIRS 512     /* max simultaneously open vdirs      */

/* ------------------------------------------------------------------ */
/* Virtual-filesystem tables                                           */
/* ------------------------------------------------------------------ */

typedef struct {
    char  path[VFS_MAX_PATH];   /* normalised absolute path             */
    int   fd;                   /* memfd fd for files; -2 for dirs      */
    int   is_dir;
    int   deleted;              /* 1 ⇒ logically removed                */
    mode_t mode;
    uid_t  uid;
    gid_t  gid;
    time_t mtime;
} ventry_t;

#define FD_DIR  (-2)
#define FD_DEAD (-1)

static ventry_t      ventries[VFS_MAX_ENTRIES];
static int           vcount      = 0;
static pthread_mutex_t vmutex   = PTHREAD_MUTEX_INITIALIZER;

/* Virtual open-dir state */
typedef struct {
    int      in_use;
    char     path[VFS_MAX_PATH];
    int      pos;
    /* snapshot of child names at opendir() time */
    char   **names;
    int      nnames;
} vdir_state_t;

static vdir_state_t  vdirs[VFS_MAX_OPEN_DIRS];
static pthread_mutex_t dirmutex = PTHREAD_MUTEX_INITIALIZER;

/* DIR* encoding: high bits signal a virtual dir, low bits are the index */
#define VDIR_MAGIC 0xCA00000000ULL
static inline DIR *encode_vdir(int idx)
    { return (DIR*)((uintptr_t)(VDIR_MAGIC | (unsigned)idx)); }
static inline int is_vdir(DIR *d)
    { return ((uintptr_t)d >> 32) == (VDIR_MAGIC >> 32); }
static inline int vdir_idx(DIR *d)
    { return (int)((uintptr_t)d & 0xFFFFFFFFULL); }

/* ------------------------------------------------------------------ */
/* Root prefix                                                          */
/* ------------------------------------------------------------------ */

static const char *MEMFS_ROOT     = NULL;
static size_t      MEMFS_ROOT_LEN = 0;
static int         memfs_debug    = 0;

#define DBG(fmt, ...) \
    do { if (memfs_debug) fprintf(stderr, "[memfs] " fmt "\n", ##__VA_ARGS__); } while(0)

static int is_virtual(const char *path) {
    if (!path || !MEMFS_ROOT) return 0;
    return strncmp(path, MEMFS_ROOT, MEMFS_ROOT_LEN) == 0 &&
           (path[MEMFS_ROOT_LEN] == '/' || path[MEMFS_ROOT_LEN] == '\0');
}

/* Normalise path: resolve ".." and "." but do NOT call realpath (which
   might try to stat non-existing dirs).  Simple stack-based canonicalisation. */
static void normalise_path(const char *in, char *out, size_t outsz) {
    /* Copy input, work in-place */
    char tmp[VFS_MAX_PATH];
    if (in[0] != '/') {
        /* relative – prepend cwd; should not normally happen for MEMFS paths */
        char *cwd = getcwd(NULL, 0);
        snprintf(tmp, sizeof(tmp), "%s/%s", cwd ? cwd : "/", in);
        free(cwd);
    } else {
        strncpy(tmp, in, sizeof(tmp)-1);
        tmp[sizeof(tmp)-1] = '\0';
    }
    /* simple component collapse */
    char *components[256];
    int nc = 0;
    char *p = tmp + 1; /* skip leading '/' */
    char *tok;
    while ((tok = strsep(&p, "/")) != NULL) {
        if (!*tok || !strcmp(tok, ".")) continue;
        if (!strcmp(tok, "..")) { if (nc > 0) nc--; }
        else if (nc < 255) components[nc++] = tok;
    }
    char *o = out;
    char *end = out + outsz - 1;
    *o++ = '/';
    for (int i = 0; i < nc; i++) {
        size_t l = strlen(components[i]);
        if (o + l + 1 > end) break;
        memcpy(o, components[i], l); o += l;
        if (i < nc-1) *o++ = '/';
    }
    *o = '\0';
}

/* ------------------------------------------------------------------ */
/* Virtual-entry lookup / creation                                      */
/* ------------------------------------------------------------------ */

/* Caller must hold vmutex */
static ventry_t *vfind(const char *path) {
    for (int i = 0; i < vcount; i++) {
        if (!ventries[i].deleted && strcmp(ventries[i].path, path) == 0)
            return &ventries[i];
    }
    return NULL;
}

static ventry_t *vfind_or_create(const char *path, int is_dir, mode_t mode) {
    ventry_t *e = vfind(path);
    if (e) return e;
    if (vcount >= VFS_MAX_ENTRIES) { errno = ENOSPC; return NULL; }
    e = &ventries[vcount++];
    memset(e, 0, sizeof(*e));
    strncpy(e->path, path, VFS_MAX_PATH-1);
    e->is_dir  = is_dir;
    e->mode    = mode;
    e->uid     = getuid();
    e->gid     = getgid();
    e->mtime   = time(NULL);
    e->deleted = 0;
    if (is_dir) {
        e->fd = FD_DIR;
    } else {
        char name[64];
        const char *bn = strrchr(path, '/');
        snprintf(name, sizeof(name), "memfs:%s", bn ? bn+1 : path);
        e->fd = memfd_create(name, 0);
        if (e->fd < 0) { e->deleted = 1; vcount--; return NULL; }
    }
    DBG("created %s %s", is_dir ? "dir" : "file", path);
    return e;
}

/* Ensure the parent directory entry exists (create virtual dirs on demand) */
static void ensure_parent_dirs(const char *path) {
    char tmp[VFS_MAX_PATH];
    strncpy(tmp, path, sizeof(tmp)-1);
    char *last = strrchr(tmp, '/');
    if (!last || last == tmp) return;
    *last = '\0';
    if (!is_virtual(tmp)) return;
    ventry_t *e = vfind(tmp);
    if (!e) {
        ensure_parent_dirs(tmp);
        vfind_or_create(tmp, 1, 0755);
    }
}

/* ------------------------------------------------------------------ */
/* Real function pointers (loaded once at init)                        */
/* ------------------------------------------------------------------ */

#define LOAD(sym) \
    do { real_##sym = dlsym(RTLD_NEXT, #sym); } while(0)

static int    (*real_open)(const char *, int, ...)             = NULL;
static int    (*real_openat)(int, const char *, int, ...)      = NULL;
static int    (*real_creat)(const char *, mode_t)              = NULL;
static FILE  *(*real_fopen)(const char *, const char *)        = NULL;
static FILE  *(*real_fopen64)(const char *, const char *)      = NULL;
static FILE  *(*real_freopen)(const char*, const char*, FILE*) = NULL;
static int    (*real_stat)(const char *, struct stat *)        = NULL;
static int    (*real_lstat)(const char *, struct stat *)       = NULL;
static int    (*real_fstatat)(int, const char *, struct stat *, int) = NULL;
static int    (*real_access)(const char *, int)                = NULL;
static int    (*real_faccessat)(int, const char *, int, int)   = NULL;
static int    (*real_rename)(const char *, const char *)       = NULL;
static int    (*real_renameat)(int, const char *, int, const char *) = NULL;
static int    (*real_unlink)(const char *)                     = NULL;
static int    (*real_unlinkat)(int, const char *, int)         = NULL;
static int    (*real_remove)(const char *)                     = NULL;
static int    (*real_mkdir)(const char *, mode_t)              = NULL;
static int    (*real_mkdirat)(int, const char *, mode_t)       = NULL;
static int    (*real_rmdir)(const char *)                      = NULL;
static DIR   *(*real_opendir)(const char *)                    = NULL;
static struct dirent *(*real_readdir)(DIR *)                   = NULL;
static int    (*real_closedir)(DIR *)                          = NULL;
static void   (*real_rewinddir)(DIR *)                         = NULL;

static void __attribute__((constructor)) memfs_init(void) {
    MEMFS_ROOT = getenv("MEMFS_ROOT");
    if (!MEMFS_ROOT) MEMFS_ROOT = "/cado-work";
    MEMFS_ROOT_LEN = strlen(MEMFS_ROOT);
    memfs_debug = (getenv("MEMFS_DEBUG") != NULL);

    LOAD(open); LOAD(openat); LOAD(creat);
    LOAD(fopen); LOAD(fopen64); LOAD(freopen);
    LOAD(stat);  LOAD(lstat);   LOAD(fstatat);
    LOAD(access); LOAD(faccessat);
    LOAD(rename); LOAD(renameat);
    LOAD(unlink); LOAD(unlinkat); LOAD(remove);
    LOAD(mkdir);  LOAD(mkdirat); LOAD(rmdir);
    LOAD(opendir); LOAD(readdir); LOAD(closedir); LOAD(rewinddir);

    /* Pre-create the root directory */
    pthread_mutex_lock(&vmutex);
    vfind_or_create(MEMFS_ROOT, 1, 0755);
    pthread_mutex_unlock(&vmutex);

    DBG("initialised, root=%s", MEMFS_ROOT);
}

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

/* Populate a struct stat from a ventry */
static void vstat_fill(ventry_t *e, struct stat *st) {
    memset(st, 0, sizeof(*st));
    st->st_mode  = e->is_dir ? (S_IFDIR | e->mode) : (S_IFREG | e->mode);
    st->st_uid   = e->uid;
    st->st_gid   = e->gid;
    st->st_mtime = e->mtime;
    st->st_atime = e->mtime;
    st->st_ctime = e->mtime;
    st->st_nlink = 1;
    if (!e->is_dir && e->fd >= 0) {
        struct stat tmp;
        if (fstat(e->fd, &tmp) == 0) st->st_size = tmp.st_size;
    }
    st->st_blksize = 4096;
    st->st_blocks  = (st->st_size + 511) / 512;
}

/* Open flags parsing */
static int flags_wants_create(int flags) {
    return (flags & O_CREAT) || (flags & O_WRONLY) || (flags & O_RDWR);
}

/* ------------------------------------------------------------------ */
/* open / openat / creat                                               */
/* ------------------------------------------------------------------ */

int open(const char *path, int flags, ...) {
    mode_t mode = 0666;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags); mode = va_arg(ap, int); va_end(ap);
    }
    char norm[VFS_MAX_PATH];
    normalise_path(path, norm, sizeof(norm));
    if (!is_virtual(norm)) return real_open(path, flags, mode);

    pthread_mutex_lock(&vmutex);
    ensure_parent_dirs(norm);
    ventry_t *e = vfind(norm);
    if (!e) {
        if (flags & O_CREAT) e = vfind_or_create(norm, 0, mode & 0777);
        else { pthread_mutex_unlock(&vmutex); errno = ENOENT; return -1; }
    }
    if (!e) { pthread_mutex_unlock(&vmutex); errno = ENOSPC; return -1; }
    if (e->is_dir) { pthread_mutex_unlock(&vmutex); errno = EISDIR; return -1; }
    if (e->deleted) {
        if (flags & O_CREAT) { e->deleted = 0; }
        else { pthread_mutex_unlock(&vmutex); errno = ENOENT; return -1; }
    }

    /* Dup the memfd so the caller gets its own independent fd position */
    int dupfd = dup(e->fd);
    if (dupfd < 0) { pthread_mutex_unlock(&vmutex); return -1; }

    if (flags & O_TRUNC) { int _r = ftruncate(dupfd, 0); (void)_r; }
    if (flags & O_APPEND) lseek(dupfd, 0, SEEK_END);
    else if (!(flags & O_WRONLY) && !(flags & O_RDWR) && !(flags & O_APPEND))
        lseek(dupfd, 0, SEEK_SET); /* read: start from beginning */
    else if (!(flags & O_APPEND))
        lseek(dupfd, 0, SEEK_SET);

    e->mtime = time(NULL);
    pthread_mutex_unlock(&vmutex);
    DBG("open %s fd=%d", norm, dupfd);
    return dupfd;
}

int openat(int dirfd, const char *path, int flags, ...) {
    mode_t mode = 0666;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags); mode = va_arg(ap, int); va_end(ap);
    }
    /* Resolve relative path via /proc/self/fd if needed */
    if (path && path[0] != '/') {
        char base[VFS_MAX_PATH];
        if (dirfd == AT_FDCWD) {
            if (!getcwd(base, sizeof(base))) return real_openat(dirfd, path, flags, mode);
        } else {
            char proc[64];
            snprintf(proc, sizeof(proc), "/proc/self/fd/%d", dirfd);
            ssize_t n = readlink(proc, base, sizeof(base)-1);
            if (n < 0) return real_openat(dirfd, path, flags, mode);
            base[n] = '\0';
        }
        char full[VFS_MAX_PATH];
        snprintf(full, sizeof(full), "%s/%s", base, path);
        return open(full, flags, mode);
    }
    return open(path, flags, mode);
}

/* Python (built with _FILE_OFFSET_BITS=64) calls open64/openat64.
   We must intercept these explicitly since our shim is built WITHOUT
   _FILE_OFFSET_BITS=64 (to avoid symbol conflicts).                  */
int open64(const char *path, int flags, ...) {
    mode_t mode = 0666;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags); mode = va_arg(ap, int); va_end(ap);
    }
    return open(path, flags, mode);
}

int openat64(int dirfd, const char *path, int flags, ...) {
    mode_t mode = 0666;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags); mode = va_arg(ap, int); va_end(ap);
    }
    return openat(dirfd, path, flags, mode);
}

int creat(const char *path, mode_t mode) {
    return open(path, O_CREAT|O_WRONLY|O_TRUNC, mode);
}
int creat64(const char *path, mode_t mode) {
    return open(path, O_CREAT|O_WRONLY|O_TRUNC, mode);
}

/* Python also uses fopen64 for some internal I/O */
FILE *fopen64(const char *path, const char *mode) { return fopen(path, mode); }
FILE *freopen64(const char *p, const char *m, FILE *s) { return freopen(p, m, s); }

/* ------------------------------------------------------------------ */
/* fopen / fopen64 / freopen                                           */
/* ------------------------------------------------------------------ */

FILE *fopen(const char *path, const char *mode) {
    char norm[VFS_MAX_PATH];
    normalise_path(path, norm, sizeof(norm));
    if (!is_virtual(norm)) return real_fopen(path, mode);

    int flags = 0;
    if (strchr(mode, 'r') && !strchr(mode, '+')) flags = O_RDONLY;
    else if (strchr(mode, 'w'))  flags = O_WRONLY|O_CREAT|O_TRUNC;
    else if (strchr(mode, 'a'))  flags = O_WRONLY|O_CREAT|O_APPEND;
    else                          flags = O_RDWR|O_CREAT;

    int fd = open(norm, flags, 0666);
    if (fd < 0) return NULL;
    /* Convert fd to FILE* */
    FILE *f = fdopen(fd, mode);
    if (!f) close(fd);
    return f;
}
FILE *freopen(const char *path, const char *mode, FILE *stream) {
    if (!path) return real_freopen(path, mode, stream);
    char norm[VFS_MAX_PATH];
    normalise_path(path, norm, sizeof(norm));
    if (!is_virtual(norm)) return real_freopen(path, mode, stream);
    FILE *f = fopen(norm, mode);
    if (!f) return NULL;
    fclose(stream);
    return f;
}

/* ------------------------------------------------------------------ */
/* stat / lstat / fstatat                                              */
/* ------------------------------------------------------------------ */

int stat(const char *path, struct stat *st) {
    char norm[VFS_MAX_PATH];
    normalise_path(path, norm, sizeof(norm));
    if (!is_virtual(norm)) return real_stat(path, st);
    pthread_mutex_lock(&vmutex);
    ventry_t *e = vfind(norm);
    if (!e) { pthread_mutex_unlock(&vmutex); errno = ENOENT; return -1; }
    vstat_fill(e, st); pthread_mutex_unlock(&vmutex); return 0;
}
int lstat(const char *path, struct stat *st) { return stat(path, st); }

/* glibc 2.33+ uses stat64/lstat64 (struct stat64 == struct stat on x86-64).
   We export these via an assembly alias to avoid the C-level type conflict. */
__asm__(".globl stat64\nstat64 = stat");
__asm__(".globl lstat64\nlstat64 = lstat");

/* glibc uses __xstat / __xstat64 internally */
int __xstat(int ver, const char *path, struct stat *st)   { (void)ver; return stat(path,st); }
int __xstat64(int ver, const char *path, struct stat *st) { (void)ver; return stat(path,st); }
int __lxstat(int ver, const char *path, struct stat *st)  { (void)ver; return lstat(path,st); }
int __lxstat64(int ver, const char *path, struct stat *st){ (void)ver; return lstat(path,st); }

int fstatat(int dfd, const char *path, struct stat *st, int flags) {
    if (!path || path[0] == '/') return stat(path, st);
    /* resolve relative */
    char base[VFS_MAX_PATH];
    if (dfd == AT_FDCWD) {
        if (!getcwd(base, sizeof(base))) return real_fstatat(dfd, path, st, flags);
    } else {
        char proc[64];
        snprintf(proc, sizeof(proc), "/proc/self/fd/%d", dfd);
        ssize_t n = readlink(proc, base, sizeof(base)-1);
        if (n < 0) return real_fstatat(dfd, path, st, flags);
        base[n] = '\0';
    }
    char full[VFS_MAX_PATH];
    snprintf(full, sizeof(full), "%s/%s", base, path);
    return stat(full, st);
}

/* ------------------------------------------------------------------ */
/* access / faccessat                                                  */
/* ------------------------------------------------------------------ */

int access(const char *path, int mode) {
    char norm[VFS_MAX_PATH];
    normalise_path(path, norm, sizeof(norm));
    if (!is_virtual(norm)) return real_access(path, mode);
    pthread_mutex_lock(&vmutex);
    ventry_t *e = vfind(norm);
    pthread_mutex_unlock(&vmutex);
    if (!e) { errno = ENOENT; return -1; }
    return 0;
}
int faccessat(int dfd, const char *path, int mode, int flags) {
    (void)dfd; (void)flags;
    return access(path, mode);
}

/* ------------------------------------------------------------------ */
/* rename / renameat                                                   */
/* ------------------------------------------------------------------ */

int rename(const char *oldpath, const char *newpath) {
    char no[VFS_MAX_PATH], nn[VFS_MAX_PATH];
    normalise_path(oldpath, no, sizeof(no));
    normalise_path(newpath, nn, sizeof(nn));
    int vold = is_virtual(no), vnew = is_virtual(nn);
    if (!vold && !vnew) return real_rename(oldpath, newpath);
    if (vold != vnew) { errno = EXDEV; return -1; }
    pthread_mutex_lock(&vmutex);
    ventry_t *e = vfind(no);
    if (!e) { pthread_mutex_unlock(&vmutex); errno = ENOENT; return -1; }
    /* Remove destination if it exists */
    ventry_t *dst = vfind(nn);
    if (dst && dst != e) {
        if (!dst->is_dir && dst->fd >= 0) close(dst->fd);
        dst->deleted = 1;
    }
    strncpy(e->path, nn, VFS_MAX_PATH-1);
    e->mtime = time(NULL);
    pthread_mutex_unlock(&vmutex);
    DBG("rename %s → %s", no, nn);
    return 0;
}
int renameat(int od, const char *op, int nd, const char *np) {
    (void)od; (void)nd; return rename(op, np);
}
int renameat2(int od, const char *op, int nd, const char *np, unsigned int f) {
    (void)od; (void)nd; (void)f; return rename(op, np);
}

/* ------------------------------------------------------------------ */
/* unlink / unlinkat / remove                                          */
/* ------------------------------------------------------------------ */

int unlink(const char *path) {
    char norm[VFS_MAX_PATH];
    normalise_path(path, norm, sizeof(norm));
    if (!is_virtual(norm)) return real_unlink(path);
    pthread_mutex_lock(&vmutex);
    ventry_t *e = vfind(norm);
    if (!e) { pthread_mutex_unlock(&vmutex); errno = ENOENT; return -1; }
    e->deleted = 1;
    pthread_mutex_unlock(&vmutex);
    return 0;
}
int unlinkat(int dfd, const char *path, int flags) {
    (void)dfd; (void)flags; return unlink(path);
}
int remove(const char *path) { return unlink(path); }

/* ------------------------------------------------------------------ */
/* mkdir / mkdirat / rmdir                                             */
/* ------------------------------------------------------------------ */

int mkdir(const char *path, mode_t mode) {
    char norm[VFS_MAX_PATH];
    normalise_path(path, norm, sizeof(norm));
    if (!is_virtual(norm)) return real_mkdir(path, mode);
    pthread_mutex_lock(&vmutex);
    ventry_t *e = vfind(norm);
    if (e) { pthread_mutex_unlock(&vmutex); errno = EEXIST; return -1; }
    ensure_parent_dirs(norm);
    e = vfind_or_create(norm, 1, mode & 0777);
    pthread_mutex_unlock(&vmutex);
    return e ? 0 : -1;
}
int mkdirat(int dfd, const char *path, mode_t mode) { (void)dfd; return mkdir(path, mode); }
int rmdir(const char *path) { return unlink(path); }

/* ------------------------------------------------------------------ */
/* opendir / readdir / closedir / rewinddir                            */
/* ------------------------------------------------------------------ */

DIR *opendir(const char *path) {
    char norm[VFS_MAX_PATH];
    normalise_path(path, norm, sizeof(norm));
    if (!is_virtual(norm)) return real_opendir(path);

    pthread_mutex_lock(&vmutex);
    ventry_t *e = vfind(norm);
    if (!e || !e->is_dir) {
        pthread_mutex_unlock(&vmutex);
        errno = e ? ENOTDIR : ENOENT;
        return NULL;
    }

    /* Collect children names */
    int n = 0;
    size_t norm_len = strlen(norm);
    char **names = NULL;
    for (int i = 0; i < vcount; i++) {
        ventry_t *c = &ventries[i];
        if (c->deleted) continue;
        if (c == e) continue;
        size_t clen = strlen(c->path);
        if (clen <= norm_len) continue;
        if (strncmp(c->path, norm, norm_len) != 0) continue;
        if (c->path[norm_len] != '/') continue;
        /* immediate child: no more '/' after the separator */
        const char *rest = c->path + norm_len + 1;
        if (strchr(rest, '/')) continue;
        names = realloc(names, (n+1) * sizeof(char*));
        if (!names) break;
        names[n++] = strdup(rest);
    }
    pthread_mutex_unlock(&vmutex);

    pthread_mutex_lock(&dirmutex);
    int idx = -1;
    for (int i = 0; i < VFS_MAX_OPEN_DIRS; i++) {
        if (!vdirs[i].in_use) { idx = i; break; }
    }
    if (idx < 0) {
        pthread_mutex_unlock(&dirmutex);
        for (int i = 0; i < n; i++) free(names[i]);
        free(names);
        errno = ENOMEM;
        return NULL;
    }
    vdir_state_t *vd = &vdirs[idx];
    vd->in_use = 1;
    strncpy(vd->path, norm, VFS_MAX_PATH-1);
    vd->pos    = 0;
    vd->names  = names;
    vd->nnames = n;
    pthread_mutex_unlock(&dirmutex);
    return encode_vdir(idx);
}

/* We need a static dirent buffer to return pointers from readdir */
static __thread struct dirent vdirent_buf;

struct dirent *readdir(DIR *d) {
    if (!is_vdir(d)) return real_readdir(d);
    int idx = vdir_idx(d);
    if (idx < 0 || idx >= VFS_MAX_OPEN_DIRS) { errno = EBADF; return NULL; }
    vdir_state_t *vd = &vdirs[idx];
    if (!vd->in_use) { errno = EBADF; return NULL; }

    if (vd->pos >= vd->nnames) return NULL;

    memset(&vdirent_buf, 0, sizeof(vdirent_buf));
    strncpy(vdirent_buf.d_name, vd->names[vd->pos], sizeof(vdirent_buf.d_name)-1);
    vdirent_buf.d_type = DT_REG; /* approximate; dirs handled by separate entry */

    /* Check if this name is a directory */
    char full[VFS_MAX_PATH];
    snprintf(full, sizeof(full), "%s/%s", vd->path, vd->names[vd->pos]);
    pthread_mutex_lock(&vmutex);
    ventry_t *e = vfind(full);
    if (e && e->is_dir) vdirent_buf.d_type = DT_DIR;
    pthread_mutex_unlock(&vmutex);

    vd->pos++;
    return &vdirent_buf;
}


int closedir(DIR *d) {
    if (!is_vdir(d)) return real_closedir(d);
    int idx = vdir_idx(d);
    if (idx < 0 || idx >= VFS_MAX_OPEN_DIRS) { errno = EBADF; return -1; }
    pthread_mutex_lock(&dirmutex);
    vdir_state_t *vd = &vdirs[idx];
    for (int i = 0; i < vd->nnames; i++) free(vd->names[i]);
    free(vd->names);
    memset(vd, 0, sizeof(*vd));
    pthread_mutex_unlock(&dirmutex);
    return 0;
}

void rewinddir(DIR *d) {
    if (!is_vdir(d)) { real_rewinddir(d); return; }
    int idx = vdir_idx(d);
    if (idx >= 0 && idx < VFS_MAX_OPEN_DIRS) vdirs[idx].pos = 0;
}

/* Python (built with _FILE_OFFSET_BITS=64) calls readdir64 and closedir.
   We alias them to our readdir/closedir implementations.               */
__asm__(".globl readdir64\nreaddir64 = readdir");

/* fdopendir: we don't intercept fd-level dirs; just return a real opendir
   on /proc/self/fd/<n> so callers get an actual (but empty) directory.
   For virtual directories this is not called in practice. */
DIR *fdopendir(int fd) {
    char proc[64];
    snprintf(proc, sizeof(proc), "/proc/self/fd/%d", fd);
    return real_opendir(proc);
}

/* ------------------------------------------------------------------ */
/* Utility: list virtual dir contents for Python glob support         */
/* ------------------------------------------------------------------ */
/* scandir — build child list in one shot */
int scandir(const char *path, struct dirent ***namelist,
            int (*filter)(const struct dirent *),
            int (*compar)(const struct dirent **, const struct dirent **)) {
    DIR *d = opendir(path);
    if (!d) return -1;
    int count = 0;
    struct dirent **list = NULL;
    struct dirent *de;
    while ((de = readdir(d)) != NULL) {
        if (filter && !filter(de)) continue;
        struct dirent *copy = malloc(sizeof(struct dirent));
        if (!copy) break;
        memcpy(copy, de, sizeof(struct dirent));
        list = realloc(list, (count+1)*sizeof(struct dirent*));
        list[count++] = copy;
    }
    closedir(d);
    if (compar) qsort(list, count, sizeof(struct dirent*),
                      (int(*)(const void*,const void*))compar);
    *namelist = list;
    return count;
}

/* ------------------------------------------------------------------ */
/* symlink / readlink stubs (CADO sometimes creates symlinks)         */
/* ------------------------------------------------------------------ */
int symlink(const char *target, const char *linkpath) {
    char norm[VFS_MAX_PATH];
    normalise_path(linkpath, norm, sizeof(norm));
    if (!is_virtual(norm)) {
        /* call real symlink */
        typedef int(*fn_t)(const char*, const char*);
        fn_t real = dlsym(RTLD_NEXT, "symlink");
        if (real) return real(target, linkpath);
        errno = EPERM; return -1;
    }
    /* Store as a regular file containing the target path */
    int fd = open(norm, O_CREAT|O_WRONLY|O_TRUNC, 0777);
    if (fd < 0) return -1;
    ssize_t _w = write(fd, target, strlen(target)); (void)_w;
    close(fd);
    return 0;
}

ssize_t readlink(const char *path, char *buf, size_t bufsz) {
    char norm[VFS_MAX_PATH];
    normalise_path(path, norm, sizeof(norm));
    if (!is_virtual(norm)) {
        typedef ssize_t(*fn_t)(const char*, char*, size_t);
        fn_t real = dlsym(RTLD_NEXT, "readlink");
        if (real) return real(path, buf, bufsz);
        errno = ENOENT; return -1;
    }
    /* Read the symlink target stored as file content */
    int fd = open(norm, O_RDONLY);
    if (fd < 0) { errno = ENOENT; return -1; }
    ssize_t n = read(fd, buf, bufsz);
    close(fd);
    return n;
}
