#include <zlib.h>

#include <cstring>

int main() {
    unsigned char out[64];
    uLongf outlen = sizeof(out);
    const char* data = "hello";
    compress(out, &outlen, reinterpret_cast<const Bytef*>(data), std::strlen(data));
    return 0;
}
