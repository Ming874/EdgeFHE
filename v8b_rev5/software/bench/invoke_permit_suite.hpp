// Invocation permit for the verification-suite tools (same rule as the accepted ef_app_v5_pl_check permit): the root
// controller writes "<TAG>\n<mode>\n<boot>\n<controller pid>\n<exe dev>\n<exe inode>\n" to a root-only 0600 file and
// passes a read-only descriptor; the tool checks it before opening the device and closes it.
#pragma once
#include <fcntl.h>
#include <fstream>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
namespace suite_permit {
inline void need(bool ok) { if (!ok) throw std::runtime_error("suite permit refused"); }
inline void check(int fd, const std::string& tag, const std::string& mode) {
  need(fd >= 3 && fd <= 1024 && ::geteuid() == 0 && ::getuid() == 0);
  struct stat st{}, exe{}; need(::fstat(fd, &st) == 0 && ::stat("/proc/self/exe", &exe) == 0 && st.st_size > 0 && st.st_size < 1024);
  char bytes[1024]{}; need(::pread(fd, bytes, size_t(st.st_size), 0) == st.st_size);
  std::ifstream file("/proc/sys/kernel/random/boot_id"); std::string boot; std::getline(file, boot); need(bool(file) && boot.size() == 36);
  const int flags = ::fcntl(fd, F_GETFL); need(flags >= 0 && (flags & O_ACCMODE) == O_RDONLY);
  need(S_ISREG(st.st_mode) && st.st_uid == 0 && (st.st_mode & 07777) == 0600 && st.st_nlink == 1);
  const std::string want = tag + '\n' + mode + '\n' + boot + '\n' + std::to_string(::getppid()) + '\n' + std::to_string(exe.st_dev) + '\n' + std::to_string(exe.st_ino) + '\n';
  need(std::string(bytes, size_t(st.st_size)) == want);
  need(::close(fd) == 0);
}
}  // namespace suite_permit
