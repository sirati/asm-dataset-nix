/* Target-sized aliases absent from early Clang, required by current libstdc++.
 * These expand using the selected target, including -m32 and MIPS N32.
 */
#pragma once
#ifndef __PTRDIFF_MAX__
#define __PTRDIFF_MAX__ (__SIZE_MAX__ >> 1)
#endif
#ifndef __UINTPTR_TYPE__
#define __UINTPTR_TYPE__ __SIZE_TYPE__
#endif

/* Clang before 3.9 cannot encode GCC's abi_tag attribute. The dual-ABI GNU
 * runtime retains the original string ABI. Explicit caller choices take
 * precedence because command-line macros are processed before this header.
 */
#ifdef __cplusplus
#ifndef _GLIBCXX_USE_CXX11_ABI
#define _GLIBCXX_USE_CXX11_ABI 0
#endif
#endif
